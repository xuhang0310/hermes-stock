"""说人话改稿：定时报告推送飞书前的最后一道编辑工序。

数字人跑完几十次工具调用后写出的报告，常把任务提示里的核对规矩原样复述进正文
（“不跨账户挪用”“不构成新授权”“不冒充……”），同一句提醒反复出现，还夹着测算、实仓、
口径这类底稿用语。这里用一次独立的 LLM 调用只改说法、不改事实，再用确定性检查兜底：

- 不许出现原文没有的关键数字（防编造，也防四舍五入、换单位）；
- 原文的股票代码一个不能少，其余关键数字最多丢 2%（合并重复提醒时允许少量出入）；
- 篇幅不能缩到原文的 35% 以下；模型因长度截断（finish_reason=length）也算失败。

开关关闭、篇幅不合适、调用失败或检查不过，一律原样返回原文——改稿是增强项，绝不阻断推送。
原文仍保存在定时任务的会话记录里，可以对照。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.ai.llm_client import chat_completion
from backend.app.core.applog import get_logger
from backend.app.core.llm_config import active_llm_config, reasoning_params
from backend.app.core.settings import settings

logger = get_logger("plain_rewrite")

MIN_CHARS = 600  # 短消息本来就好读，不值得多一次模型调用
MAX_CHARS = 16000  # 超长报告一次改不完、容易被截断，直接用原文
_MIN_LENGTH_RATIO = 0.35
_MISSING_TOLERANCE = 0.02

EDITOR_PROMPT = """你是投资研究团队的文字编辑。下面这份报告会推送到飞书，读者是投资者本人。报告里的内容和数字已经核对过，你只改说法，让人一眼看懂、愿意读完。

一、事实一点不能动
1. 每个账户、每只股票（名称和代码）、每个价位、股数、金额、涨跌幅、事件日期都要保留；数字原样照抄，不四舍五入，不换单位，不自己算合计或差额。
2. 结论、操作条件、风险、还没核实的事项都要保留，只改写法，不改意思，也不加原文没有的判断。
3. 表格可以保留，表头和单元格可以改成白话；一张表最多 4 列。

二、要去掉的“AI 腔”
1. 复述规矩、自证清白的话，如“不冒充……”“不跨账户挪用”“不构成新授权”“不是挂单”“不把建议当成交”“不预支卖出所得”。规矩照做了就行，读者不需要知道你遵守了哪些规则。
2. 同一个提醒反复说：数据还没核实、只是参考、可能成交不了之类，全文只说一次。没核实的事项集中放到末尾一段，说清楚会影响哪些操作。
3. 工作底稿用语：测算、核验、口径、覆盖、实仓、样例记录、旧快照、申报数量、剩余额度、待核实、第一批/第二批等，换成大白话。比如“第一批测算”写成“先卖”，“待核实”写成“确认能卖再动”，“申报数量”写成“最少要卖多少股”。
4. 每行都挂时间点：数据是几点的，在开头或末尾统一交代一次；只有不同数据的时间不一样、而且影响判断时，才在正文里提。
5. 成串的否定句和双重否定，如“不是……也不是……不能据此……”，改成直接说该怎么做。
6. 过程汇报：查了哪些数据、调用了什么、推送有没有成功、数据是怎么补齐的，与结论无关的全部删掉。
7. 堆在一起的长句：一句话里有两个以上分号，就拆成几句或列表。

三、写法
1. 开头三行内说清最重要的结论：现在是什么情况、最该盯哪几只、要不要动、为什么。
2. 称呼读者为“你”，用短句和口语，像懂行的朋友当面讲。
3. 小标题本身就是一句白话结论，不用“风险提示”“数据说明”这类栏目名。
4. 结尾保留一句“以上是研究参考，不构成投资建议。”，其他免责声明不再写。

只输出改好的报告正文（Markdown），不要解释你改了哪里。"""

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")
_UNITS = "%％股元万亿倍手"
_YEAR = re.compile(r"20\d\d")
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)


@dataclass(frozen=True)
class PlainRewrite:
    text: str
    applied: bool
    reason: str = ""  # 尝试改稿但没采用时的原因；没尝试或已采用时为空


def rewrite_plain(text: str) -> PlainRewrite:
    """把一份报告改成白话；任何不确定都返回原文。"""
    original = (text or "").strip()
    if not settings.plain_rewrite_enabled or not MIN_CHARS <= len(original) <= MAX_CHARS:
        return PlainRewrite(text, False)
    try:
        llm_config = active_llm_config()
        params = reasoning_params(llm_config)
        response = chat_completion(
            [
                {"role": "system", "content": EDITOR_PROMPT},
                {"role": "user", "content": original},
            ],
            llm_config,
            tools=None,
            tool_choice=None,
            enable_thinking=params.get("enable_thinking"),
            reasoning_effort=params.get("reasoning_effort"),
            timeout=300.0,
            log_module="plain_rewrite",
        )
        choice = (response.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            return PlainRewrite(text, False, "改稿被长度截断")
        edited = str((choice.get("message") or {}).get("content") or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 改稿是增强项，失败必须静默降级，不能阻断推送
        logger.warning("说人话改稿失败，沿用原文: %s", str(exc)[:200])
        return PlainRewrite(text, False, f"改稿调用失败: {str(exc)[:200]}")
    fenced = _FENCE.match(edited)
    if fenced:
        edited = fenced.group(1).strip()
    problem = check_facts_kept(original, edited)
    if problem:
        return PlainRewrite(text, False, problem)
    return PlainRewrite(edited, True)


def check_facts_kept(original: str, edited: str) -> str:
    """改稿是否保住了原文的事实；通过返回空串，否则返回不采用的原因。"""
    if not edited.strip():
        return "改稿为空"
    if len(edited) < len(original) * _MIN_LENGTH_RATIO:
        return f"改稿只剩 {len(edited)} 字（原文 {len(original)} 字），删得太多"
    before_text, after_text = _THOUSANDS.sub("", original), _THOUSANDS.sub("", edited)
    missing_codes = set(_CODE.findall(before_text)) - set(_CODE.findall(after_text))
    if missing_codes:
        return f"改稿漏了 {'、'.join(sorted(missing_codes)[:5])}"
    before, after = _key_numbers(before_text), _key_numbers(after_text)
    invented = sorted(value for value in after - before if not _YEAR.fullmatch(value))
    if invented:
        return f"改稿出现原文没有的数字 {'、'.join(invented[:5])}"
    missing = sorted(before - after)
    if len(missing) > max(2, int(len(before) * _MISSING_TOLERANCE)):
        return f"改稿漏掉 {len(missing)} 个关键数字，如 {'、'.join(missing[:5])}"
    return ""


def _key_numbers(text: str) -> set[str]:
    """价格、股数、金额、比例、代码这类关键数字；时刻、月日、序号等不带单位的一两位整数不计。"""
    found: set[str] = set()
    for match in _NUMBER.finditer(text):
        value = match.group(0)
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        elif len(value) < 3:
            unit = text[match.end():match.end() + 2].lstrip()[:1]
            if not unit or unit not in _UNITS:
                continue
        found.add(value)
    return found
