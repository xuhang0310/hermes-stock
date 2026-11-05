"""消息事件的规则：哪些算重要、什么时候推、哪两条算同一条。纯函数，不碰数据库和模型。

先用规则筛，不调模型，才能把模型调用次数控制住。重要消息的规则宁可多放，放错了模型还能判成不重要；
重大消息模型改不了，还会夜里推，规则要收紧。

分级只看标题，不看正文：季报正文都有“不存在虚假记载”这样的声明，看正文会误报成财务造假。
这些规则对照 2026-10-28 至 11-06 的真实公告和新闻调过，见 tests/test_market_events.py。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

CRITICAL = "critical"  # 立案、退市、停牌这类，夜里也推
MAJOR = "major"  # 业绩、增减持这类，白天即时推，夜里并入早盘
MINOR = "minor"  # 只提到了这只股票，不单独推，并入下一份报告

_SEVERITY_LABELS = {CRITICAL: "重大", MAJOR: "重要", MINOR: "一般"}
OTHER_TYPE = "其他"

_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (CRITICAL, "立案调查", re.compile(r"立案调查|立案告知|证监会.{0,8}立案|被调查|留置|刑事拘留|涉嫌(?:犯罪|违法|违规|信息披露)")),
    # 不写 *ST：持有 *ST 股票时，它的每条新闻都会变成重大消息
    (CRITICAL, "退市风险", re.compile(r"退市|终止上市|暂停上市|风险警示")),
    (CRITICAL, "财务造假", re.compile(r"财务造假|无法表示意见|否定意见")),
    (CRITICAL, "停牌", re.compile(r"(?<!不)停牌")),
    (
        MAJOR,
        "业绩",
        re.compile(
            r"业绩预告|业绩快报|业绩预增|业绩预减|预盈|预亏|扭亏|首亏|续亏|亏损\d"
            r"|(?:净利|营收|营业收入|盈利).{0,30}(?:同比|下降|增长|下滑|大增|翻倍|腰斩|亏)"
        ),
    ),
    (MAJOR, "再融资", re.compile(r"向特定对象发行|定增|非公开发行|配股|可转债")),
    (MAJOR, "监管", re.compile(r"问询函|监管函|警示函|关注函|监管措施|行政处罚|纪律处分|通报批评")),
    (MAJOR, "增减持", re.compile(r"减持|增持")),
    (MAJOR, "回购", re.compile(r"回购")),
    (MAJOR, "重大合同", re.compile(r"重大合同|中标|重大订单|签订.{0,12}合同")),
    (MAJOR, "诉讼", re.compile(r"诉讼|仲裁")),
    (MAJOR, "质押冻结", re.compile(r"质押|冻结|司法拍卖")),
    (MAJOR, "复牌", re.compile(r"复牌")),
    (MAJOR, "重组", re.compile(r"重组|收购|并购|重大资产")),
    (MAJOR, "分红送转", re.compile(r"分红|送转|派息|转增|权益分派")),
)
# 中介出具的文件和例行进展，标题里带“回购”“减持”也没有新信息
_ROUTINE = re.compile(r"法律意见书|核查意见|自查报告|审计报告|回购注销.{0,40}限制性股票|回购.{0,12}进展")
_NORMALIZE = re.compile(r"[\s\W_]+")
_HEAD_CHARS = 200
_MIN_NAME_CHARS = 3  # 两个字的名字太容易误中，只认代码
_INTERPRET_TEXT_CHARS = 160
_FETCH_DAY = (time(7, 0), time(15, 30))
_FETCH_EVENING = (time(19, 0), time(22, 0))
_PUSH_HOURS = (time(7, 30), time(22, 30))
DIRECTIONS = {"positive", "negative", "neutral"}
RELIABILITIES = {"高", "中", "低"}
# 模型输出会写进飞书卡片，去掉能拼出链接和标签的字符，新闻正文里的注入内容拼不出东西
_UNSAFE = re.compile(r"[\[\]<>`*_#|]")


@dataclass(frozen=True)
class Classification:
    event_type: str
    severity: str


def severity_label(severity: str) -> str:
    return _SEVERITY_LABELS.get(severity, severity)


def classify(title: str) -> Classification:
    for severity, event_type, pattern in _RULES:
        if pattern.search(title):
            if severity == MAJOR and _ROUTINE.search(title):
                return Classification(event_type, MINOR)
            return Classification(event_type, severity)
    return Classification(OTHER_TYPE, MINOR)


def head_of(title: str, content: str | None) -> str:
    """匹配只看标题和开头：长篇综述的后半段顺带提一句，不算这只股票的消息。"""
    return f"{title} {(content or '')[:_HEAD_CHARS]}"


def mentions(text: str, symbol: str, name: str) -> bool:
    if re.search(rf"(?<!\d){re.escape(symbol.split('.')[0])}(?!\d)", text):
        return True
    clean = (name or "").strip()
    return len(clean) >= _MIN_NAME_CHARS and clean in text


def event_hash(symbol: str, title: str) -> str:
    """同一只股票的同一条消息只算一次。多个来源转发时标题常常只差标点。"""
    normalized = _NORMALIZE.sub("", title or "")[:60]
    return hashlib.sha256(f"{symbol}:{normalized}".encode("utf-8")).hexdigest()[:64]


def announcement_dates(today: date) -> list[str]:
    """晚上发的公告日期常是第二个交易日，今天和明天都要查。"""
    return [today.strftime("%Y%m%d"), (today + timedelta(days=1)).strftime("%Y%m%d")]


def should_fetch_announcements(now: datetime) -> bool:
    """盘前和交易时段每轮都拉，晚上每 30 分钟，其他时间每小时，夜里的重大消息也不会漏太久。"""
    clock = now.time()
    if _FETCH_DAY[0] <= clock <= _FETCH_DAY[1]:
        return True
    if _FETCH_EVENING[0] <= clock <= _FETCH_EVENING[1]:
        return now.minute % 30 < 15
    return now.minute < 15


def should_push_now(severity: str, now: datetime, *, night_push: bool) -> bool:
    if _PUSH_HOURS[0] <= now.time() <= _PUSH_HOURS[1]:
        return severity in (CRITICAL, MAJOR)
    return severity == CRITICAL and night_push


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(_UNSAFE.sub("", str(value or "")).split())[:limit]


def parse_interpretation(text: str) -> dict[str, Any] | None:
    """解析模型的 JSON 答复；格式不对或缺结论时返回 None，调用方按没解读处理。"""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    summary = _clean_text(data.get("summary"), _INTERPRET_TEXT_CHARS)
    if not summary:
        return None
    direction = str(data.get("direction") or "").strip().lower()
    reliability = str(data.get("reliability") or "").strip()
    return {
        "summary": summary,
        "direction": direction if direction in DIRECTIONS else "neutral",
        "reliability": reliability if reliability in RELIABILITIES else "中",
        "impact": _clean_text(data.get("impact"), 300),
        "plan_effect": _clean_text(data.get("plan_effect"), 300),
        "important": data.get("important") is not False,
    }
