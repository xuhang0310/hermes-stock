"""选股器 AI 能力：自然语言转选股条件 + 选股结果解读（含前 10 只逐只点评）。

单一职责：只负责 LLM 调用与 JSON 解析/校验——不读写表、不调行情源、不拼 SQL
（条件 SQL 见 screener_service）。调用规范复用 ai/llm_client.chat_completion +
core/llm_config.active_llm_config（HTTP≥400 抛 RuntimeError 并自动记台账）。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from backend.app.ai.llm_client import chat_completion
from backend.app.core.llm_config import active_llm_config

_NUMERIC_FIELDS = (
    "price_min",
    "price_max",
    "pct_chg_min",
    "pct_chg_max",
    "volume_ratio_min",
    "turnover_rate_min",
    "turnover_rate_max",
    "total_mv_min",
    "total_mv_max",
    "pe_min",
    "pe_max",
)
_NONNEGATIVE_FIELDS = (
    "price_min",
    "price_max",
    "volume_ratio_min",
    "turnover_rate_min",
    "turnover_rate_max",
    "total_mv_min",
    "total_mv_max",
    "pe_min",
)
_ALLOWED_MARKETS = {"SH", "SZ", "BSE"}
_ALLOWED_MA = (5, 10, 20)
_MARKET_NAMES = {"SH": "沪市", "SZ": "深市", "BSE": "北交所"}

_PARSE_SYSTEM_PROMPT = """你是 A 股选股器条件解析助手，把用户的自然语言选股要求转成 JSON 选股条件。
可用字段（只输出用户明确提到的字段，严禁自行编造条件，数值一律用 JSON 数字）：
- market：市场，SH（沪市）/ SZ（深市）/ BSE（北交所）三者之一
- price_min / price_max：最新价下限/上限（元）
- pct_chg_min / pct_chg_max：当日涨跌幅下限/上限（%，可为负数）
- volume_ratio_min：量比下限
- turnover_rate_min / turnover_rate_max：换手率下限/上限（%）
- total_mv_min / total_mv_max：总市值下限/上限（**单位亿元**，用户说多少亿直接填数字）
- pe_min / pe_max：市盈率(TTM)下限/上限
- above_ma：收盘站上 N 日均线，5 / 10 / 20 三者之一
- keyword：股票代码或名称关键词

只输出一个 JSON 对象，不要解释、不要 markdown 包裹。示例：
{"volume_ratio_min": 2, "turnover_rate_min": 5, "turnover_rate_max": 15, "total_mv_max": 200, "above_ma": 20}"""

_INTERPRET_SYSTEM_PROMPT = """你是一位擅长把选股结果讲给普通人听的助手。用户是普通投资者。
你会收到一份选股事实（JSON）：筛选条件 query、候选池大小 universe、命中数 total、最多 15 只命中股票明细
items（含最新价/涨跌幅/量比/换手率/总市值亿/PE(TTM)，可能为 null）。请写一份大白话解读报告。

要求：
1. 结构（用这些二级标题）：
   ## 整体结构
   ## 条件解读（用通俗的话解释每个条件在选什么样的股票）
   ## 前10只逐只点评（每只 1-2 句，基于给定数字；命中不足 10 只就逐条点评全部）
   ## 风险提示
2. 只引用给定数据里的数字，严禁编造；引用时保持原精度。
3. 数据为 null 的字段不要臆测，跳过即可。
4. 语气平实坦率，可以通俗，不要油滑。
5. 必须写清楚：这只是基于行情数据的筛选结果，不代表未来走势，不构成任何投资建议。
6. 用 Markdown 输出，全文控制在 900 字以内。"""

_FALLBACK_SUMMARY = "未能从描述中解析出条件，请手动填写"

_TEMPLATES_SYSTEM_PROMPT = """你是 A 股选股器模板设计师。根据用户当前持仓的风格特征，设计能快速发现"同类但尚未持有"股票的筛选模板。
你会收到持仓事实 JSON：positions（每只含 name/position_pct/price/pct_chg/turnover_rate/volume_ratio/total_mv_yi（亿元）/pe_ttm/above_ma20，字段可能为 null）与 stats（市值/换手率/PE/涨跌幅的 min/median/max 分布及站上 20 日线的只数）。

只输出一个 JSON 对象，不要解释、不要 markdown 包裹：
{"basis": "一句话概括用户持仓风格（40字以内）", "templates": [{"name": "4-6字模板名", "desc": "一句话说明条件（30字以内）", "query": {...}}]}

要求：
1. templates 给 5-8 个，每个 query 只能包含以下字段（没提到的字段直接省略，数值一律用 JSON 数字）：
   - market：SH / SZ / BSE 三选一
   - price_min / price_max（元）；pct_chg_min / pct_chg_max（%）
   - volume_ratio_min；turnover_rate_min / turnover_rate_max（%）
   - total_mv_min / total_mv_max（亿元）；pe_min / pe_max；above_ma：5 / 10 / 20
2. 条件必须贴合持仓的真实分布（参照 stats 的 min/median/max），方向是找"与持仓同类"的股票。
3. 各模板侧重不同风格（如放量异动 / 低估值 / 小盘活跃 / 均线趋势等），条件组合不要雷同。
4. 严禁编造数据；条件宁可适度放宽也要保证能筛出股票。
5. basis 只描述给定持仓特征，严禁臆测收益建议。"""


def _extract_json(text: str) -> dict:
    """容错解析：模型可能用 ```json 包裹或前后带解释，抽取第一个 {...} 块。"""
    snippet = (text or "").strip()
    match = re.search(r"\{.*\}", snippet, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize(data: dict) -> dict:
    """把模型输出清洗成合法 ScreenerQuery：强转数值、坏枚举丢弃、负值截断、未知键忽略。"""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _NUMERIC_FIELDS:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            continue
    for key in _NONNEGATIVE_FIELDS:
        if key in out and out[key] < 0:
            out[key] = 0.0
    market = str(data.get("market") or "").strip().upper()
    if market in _ALLOWED_MARKETS:
        out["market"] = market
    try:
        above_ma = int(float(data.get("above_ma")))
        if above_ma in _ALLOWED_MA:
            out["above_ma"] = above_ma
    except (TypeError, ValueError):
        pass
    keyword = str(data.get("keyword") or "").strip()[:32]
    if keyword:
        out["keyword"] = keyword
    return out


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _summarize(query: dict) -> str:
    """把解析出的条件拼成一句中文回显，供前端展示确认。"""
    parts: list[str] = []
    if query.get("market"):
        parts.append(_MARKET_NAMES.get(query["market"], query["market"]))
    if query.get("price_min") is not None or query.get("price_max") is not None:
        lo, hi = query.get("price_min"), query.get("price_max")
        if lo is not None and hi is not None:
            parts.append(f"价格 {_fmt(lo)}–{_fmt(hi)} 元")
        elif lo is not None:
            parts.append(f"价格 ≥ {_fmt(lo)} 元")
        else:
            parts.append(f"价格 ≤ {_fmt(hi)} 元")
    if query.get("pct_chg_min") is not None or query.get("pct_chg_max") is not None:
        lo, hi = query.get("pct_chg_min"), query.get("pct_chg_max")
        if lo is not None and hi is not None:
            parts.append(f"涨跌幅 {_fmt(lo)}%–{_fmt(hi)}%")
        elif lo is not None:
            parts.append(f"涨跌幅 ≥ {_fmt(lo)}%")
        else:
            parts.append(f"涨跌幅 ≤ {_fmt(hi)}%")
    if query.get("volume_ratio_min") is not None:
        parts.append(f"量比 ≥ {_fmt(query['volume_ratio_min'])}")
    if query.get("turnover_rate_min") is not None or query.get("turnover_rate_max") is not None:
        lo, hi = query.get("turnover_rate_min"), query.get("turnover_rate_max")
        if lo is not None and hi is not None:
            parts.append(f"换手率 {_fmt(lo)}–{_fmt(hi)}%")
        elif lo is not None:
            parts.append(f"换手率 ≥ {_fmt(lo)}%")
        else:
            parts.append(f"换手率 ≤ {_fmt(hi)}%")
    if query.get("total_mv_min") is not None or query.get("total_mv_max") is not None:
        lo, hi = query.get("total_mv_min"), query.get("total_mv_max")
        if lo is not None and hi is not None:
            parts.append(f"市值 {_fmt(lo)}–{_fmt(hi)} 亿")
        elif lo is not None:
            parts.append(f"市值 ≥ {_fmt(lo)} 亿")
        else:
            parts.append(f"市值 ≤ {_fmt(hi)} 亿")
    if query.get("pe_min") is not None or query.get("pe_max") is not None:
        lo, hi = query.get("pe_min"), query.get("pe_max")
        if lo is not None and hi is not None:
            parts.append(f"PE {_fmt(lo)}–{_fmt(hi)}")
        elif lo is not None:
            parts.append(f"PE ≥ {_fmt(lo)}")
        else:
            parts.append(f"PE ≤ {_fmt(hi)}")
    if query.get("above_ma"):
        parts.append(f"站上 {query['above_ma']} 日均线")
    if query.get("keyword"):
        parts.append(f"关键词「{query['keyword']}」")
    if not parts:
        return _FALLBACK_SUMMARY
    return "已解析：" + "，".join(parts)


def _completion_content(data: dict) -> str:
    try:
        content = str((data.get("choices") or [{}])[0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        content = ""
    return content.strip()


def _llm_complete(messages: list[dict], temperature: float, timeout: float) -> dict:
    """统一 LLM 调用：chat_completion 只包 HTTP≥400，网络/超时异常转 RuntimeError（→502）。"""
    config = active_llm_config()
    try:
        data = chat_completion(
            messages, config, temperature=temperature, timeout=timeout, log_module="screener"
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("LLM 服务请求失败（超时或网络异常），请稍后重试") from exc
    return {"config": config, "data": data}


def parse_query(text: str) -> dict:
    """一句话自然语言 → ScreenerQuery 条件；解析不出时 fallback=True 且不抛异常。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请输入自然语言选股条件")
    out = _llm_complete(
        [
            {"role": "system", "content": _PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": raw},
        ],
        temperature=0.2,
        timeout=30.0,
    )
    query = _sanitize(_extract_json(_completion_content(out["data"])))
    if not query:
        return {"query": {}, "summary": _FALLBACK_SUMMARY, "fallback": True}
    return {"query": query, "summary": _summarize(query), "fallback": False}


def interpret_result(query: dict, universe: int, total: int, items: list[dict]) -> dict:
    """用当前活跃 LLM 解读选股结果：整体结构 + 条件解读 + 前 10 只点评 + 风险提示。"""
    if not isinstance(items, list) or not items:
        raise ValueError("没有可解读的选股结果，请先运行筛选")
    facts = {
        "query": query or {},
        "universe": int(universe or 0),
        "total": int(total or 0),
        "items": [dict(item) for item in items[:15] if isinstance(item, dict)],
    }
    out = _llm_complete(
        [
            {"role": "system", "content": _INTERPRET_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"以下是选股结果 JSON：\n{json.dumps(facts, ensure_ascii=False)}",
            },
        ],
        temperature=0.3,
        timeout=180.0,
    )
    content = _completion_content(out["data"])
    if not content:
        raise RuntimeError("模型未返回解读内容，请重试或更换模型")
    return {"interpretation": content, "model": out["config"].get("display_name")}


def recommend_templates(facts: dict) -> dict:
    """依据持仓风格事实生成 5-8 个筛选模板。

    返回 {basis, templates:[{name, desc, query}], model}；合法模板不足 3 个
    抛 RuntimeError（API 层转 502）。temperature 略高，让「换一批」有多样性。
    """
    if not isinstance(facts, dict) or not facts.get("positions"):
        raise ValueError("暂无持仓，无法生成智能模板")
    out = _llm_complete(
        [
            {"role": "system", "content": _TEMPLATES_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"以下是持仓事实 JSON：\n{json.dumps(facts, ensure_ascii=False)}",
            },
        ],
        temperature=0.5,
        timeout=180.0,
    )
    parsed = _extract_json(_completion_content(out["data"]))
    basis = str(parsed.get("basis") or "").strip()[:120]
    raw_templates = parsed.get("templates")
    if not isinstance(raw_templates, list):
        raise RuntimeError("未能生成有效的模板列表，请重试")
    templates = []
    for index, item in enumerate(raw_templates):
        if not isinstance(item, dict):
            continue
        query = _sanitize(item.get("query") or {})
        if not query:
            continue
        name = str(item.get("name") or "").strip()[:12] or f"风格模板{index + 1}"
        desc = str(item.get("desc") or "").strip()[:60] or _summarize(query).removeprefix("已解析：")
        templates.append({"name": name, "desc": desc, "query": query})
        if len(templates) >= 8:
            break
    if len(templates) < 3:
        raise RuntimeError("生成的有效模板不足，请重试")
    return {"basis": basis, "templates": templates, "model": out["config"].get("display_name")}
