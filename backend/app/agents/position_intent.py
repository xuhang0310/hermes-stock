"""持仓意图预处理层（P1）：在 LLM 调用前对用户输入做关键词→动作分类，提取标的/数量/价格。

职责单一：只做"检测"，不改变任何执行路径。调用方拿到 PositionIntent 后可自行决定
如何注入 system prompt 或路由工具。

设计：
- 纯函数，无 DB/网络依赖，零开销
- 正则词典驱动，可随时扩充不用改函数体
- confidence 基于模式精度（显式关键词高信心，语义推测低信心）
- 标的提取复用 normalize_a_share_symbol 保证格式一致
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PositionAction(Enum):
    BUY = "buy"              # 买入/建仓/开仓 — 新建一个标的的持仓
    SELL = "sell"            # 卖出/出掉 — 卖出部分持仓
    CLOSE = "close"          # 清仓/全部卖出/止损出场 — quantity→0, status→closed
    ADD = "add"              # 加仓/补仓/增持 — 现有持仓 quantity 增加
    REDUCE = "reduce"        # 减仓/减持/出一部分 — 现有持仓 quantity 减少
    UPDATE_FIELD = "update"  # 改止损/改止盈/改逻辑说明/改仓位
    QUERY = "query"          # 查看持仓/仓位/持有哪些
    UNKNOWN = "unknown"


@dataclass
class PositionIntent:
    action: PositionAction
    symbol: str | None = None       # 标准化后的 A 股代码，如 600519.SH
    symbol_raw: str | None = None   # 用户原始输入的标的文本（名称或代码）
    quantity: float | None = None   # 提取到的数量
    price: float | None = None      # 提取到的价格/成本价
    confidence: float = 0.0         # 置信度 0-1
    matched_keywords: list[str] = field(default_factory=list)  # 命中的关键词


# ---------------------------------------------------------------------------
# 动作关键词词典 — 按顺序匹配，先命中先胜
# 每个条目: (正则串, 置信度基数)
# ---------------------------------------------------------------------------

_ACTION_PATTERNS: dict[PositionAction, list[tuple[str, float]]] = {
    PositionAction.CLOSE: [
        (r"(清仓|全部卖出|全出|全清|都清掉|全卖了|都卖了|止损出场|止损出局|全部平仓|全平|不拿了|不要了|全抛|全扔|清掉)", 0.95),
        (r"(清掉|出清|平仓).{0,6}(持仓|仓位|票)", 0.90),
        (r"(全部|全给|全都).{0,6}(清|卖|抛|出|扔)", 0.88),
        (r"(把|将).{0,10}(清|全卖|全清).{0,6}(掉|了|完)", 0.87),
        (r"(仓位|持仓).{0,6}(全|全部).{0,4}(清|出|卖)", 0.86),
    ],
    PositionAction.BUY: [
        (r"\b(buy|BUY)\b", 0.92),
        (r"(买入|买进|建仓|开仓|入仓|进仓|新建|新开)(仓位|持仓|头寸)?", 0.90),
        (r"(想买|要买|准备买|打算买|计划买|考虑买|想入|想进)", 0.78),
        (r"^(买入|建仓|开仓)", 0.92),
        (r"(新建|新开).{0,4}持仓", 0.85),
        (r"(想买|要买|买点|买些|买一点)", 0.76),
    ],
    PositionAction.ADD: [
        (r"(加仓|补仓|增持|加码|追加|多买|再买).{0,6}(一点|一些)?", 0.90),
        (r"\b(加仓|补仓|增持)\b", 0.92),
        (r"(再买|多买|追买).{0,4}(点|些|手|股)", 0.82),
        (r"(继续买|接着买|接着加)", 0.80),
        (r"\b(加)\s*\d+", 0.84),  # "加 2000" / "加2000" / "加2000股"
    ],
    PositionAction.REDUCE: [
        (r"(减仓|减持|减一点|出一半|减半|卖一部分|出一部分|卖掉一半|卖掉一些|减一些|减一半)", 0.90),
        (r"\b(减仓|减持)\b", 0.92),
        (r"(先出|先卖).{0,3}(一半|部分|一点|一些)", 0.85),
        (r"(减掉|减去).{0,3}(一半|部分|一点|一些)", 0.84),
        (r"(减|出).{0,2}(一半|半仓).{0,2}(仓位|持仓)?", 0.83),
        (r"\b(减)\s*\d+", 0.82),  # "减 500" / "减500股"
    ],
    PositionAction.SELL: [
        (r"(卖出|卖掉|出掉|抛售|出货|抛了|甩了)", 0.88),
        (r"\b(卖出|抛售)\b", 0.90),
        (r"(想卖|要卖|准备卖|打算卖|考虑卖|想抛)", 0.78),
    ],
    PositionAction.UPDATE_FIELD: [
        (r"(改止损|设止损|调整止损|修改止损|止损调到|止损改为|止损改|止损设)", 0.92),
        (r"(改止盈|设止盈|调整止盈|修改止盈|止盈调到|止盈改为|止盈改|止盈设)", 0.92),
        (r"(止损|止盈).{0,12}(改为|调到|设为|设成|修改|调整|改|设|到|至)", 0.85),
        (r"(修改|调整|更改).{0,4}(持仓逻辑|风险说明|投资逻辑|仓位比例)", 0.85),
        (r"(把|将|帮|请).{0,12}(止损|止盈).{0,12}(改|调|设|设置|调为|改成|调整到)", 0.83),
        (r"(设个|设一个|改一下|调一下).{0,4}(止损|止盈)", 0.82),
    ],
    PositionAction.QUERY: [
        (r"(查看持仓|看持仓|我的仓位|持仓情况|有什么票|持有哪些|看看仓位|查持仓|仓位怎样)", 0.95),
        (r"(我现在|目前).{0,4}(持仓|仓位|持有)", 0.88),
        (r"^(持仓|仓位|我的持仓)\s*$", 0.92),
    ],
}


# ---------------------------------------------------------------------------
# 标的提取 — 支持多种表达方式
# ---------------------------------------------------------------------------

# 6 位 A 股代码模式
_CODE_RE = re.compile(r"\b(\d{6})(?:\.(?:SH|SZ|BJ))?\b", re.IGNORECASE)
# 完整 ts_code 模式
_TS_CODE_RE = re.compile(r"\b(\d{6}\.(?:SH|SZ|BJ))\b", re.IGNORECASE)

# 中文股票名称常见后缀（去除后再做模糊匹配）
_NAME_SUFFIX = re.compile(r"(股份|科技|集团|控股|实业|医药|银行|证券|保险|地产|汽车|电子|能源|通信|传媒|食品|饮料|电器|材料)")

# 常见 A 股名称的模式：2-4 个中文字符加可选后缀，前缀排除常见动词
_NAME_RE = re.compile(
    r"(?:[买买卖卖加加减建开清平进出补增持止损]|查看|准备|打算|计划|考虑|"
    r"现在|目前|已经|还有|剩余|仓位|持仓|多少钱|价格|最新|多少股)"
    r"?([一-鿿]{2,4}(?:股份|科技|集团|控股|实业|医药|银行|证券|保险|地产|汽车|电子|能源|通信|传媒|食品|饮料|电器|材料)?)",
)


# ---------------------------------------------------------------------------
# 数量提取
# ---------------------------------------------------------------------------

_QUANTITY_PATTERNS = [
    # "1000股" / "1000 股" / "一千股"
    (re.compile(r"(\d+(?:\.\d+)?)\s*股"), lambda m: float(m.group(1))),
    # "10手" / "10 手"
    (re.compile(r"(\d+(?:\.\d+)?)\s*手"), lambda m: float(m.group(1)) * 100),
    # "1000张" / "1000 张" (可转债等)
    (re.compile(r"(\d+(?:\.\d+)?)\s*张"), lambda m: float(m.group(1)) * 10),
    # 中文数字 + 股（简化版，只覆盖常见）
    (re.compile(r"(一|两|三|四|五|六|七|八|九|十|半)([千百十万亿]?)(股|手)"), None),
    # 纯数字在"买/卖/加/减"后面，"买1000" / "加500"
    (re.compile(r"(?:买入|卖出|买进|加仓|补仓|减仓|减持|买入量?|卖出量?|买|卖)\s*(\d+(?:\.\d+)?)(?:$|\s|，|。|,|元|块)"), lambda m: float(m.group(1))),
]


# ---------------------------------------------------------------------------
# 价格提取
# ---------------------------------------------------------------------------

_PRICE_PATTERNS = [
    # "@150.5" / "@ 150.5"
    (re.compile(r"@\s*(\d+(?:\.\d+)?)"), lambda m: float(m.group(1))),
    # "价格150" / "价格 150" / "150元" / "150块"
    (re.compile(r"(?:价格|成本|均价|成交价|买入价|卖出价)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[元块]?"), lambda m: float(m.group(1))),
    # "成本150元" / "成本 150"
    (re.compile(r"成本\s*(\d+(?:\.\d+)?)\s*[元块]?"), lambda m: float(m.group(1))),
    # "在150买入" / "150附近买"
    (re.compile(r"(?:在|以)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?\s*(?:买入|买进|建仓|入)"), lambda m: float(m.group(1))),
]


def detect_position_intent(text: str) -> PositionIntent:
    """从用户输入文本检测持仓操作意图。纯函数，无副作用。"""

    cleaned = (text or "").strip()
    if not cleaned:
        return PositionIntent(action=PositionAction.UNKNOWN)

    # 1. 动作分类 — 按优先级顺序匹配
    action_order = [
        PositionAction.CLOSE,
        PositionAction.BUY,
        PositionAction.ADD,
        PositionAction.REDUCE,
        PositionAction.SELL,
        PositionAction.UPDATE_FIELD,
        PositionAction.QUERY,
    ]

    best_action = PositionAction.UNKNOWN
    best_confidence = 0.0
    best_keywords: list[str] = []

    for action in action_order:
        patterns = _ACTION_PATTERNS.get(action, [])
        for pattern_str, base_conf in patterns:
            match = re.search(pattern_str, cleaned, re.IGNORECASE)
            if match:
                # 按优先级顺序：只要找到就选中（不再按置信度比大小），高优先级动作先胜
                # 但仍取同 action 内最高置信度的匹配关键词
                conf = base_conf
                if best_action == PositionAction.UNKNOWN or action.value == best_action.value:
                    best_action = action
                    if conf > best_confidence:
                        best_confidence = conf
                        best_keywords = [match.group(0)]
                # 如果是更高级别的 action 找到了匹配，直接跳出
                if best_action != PositionAction.UNKNOWN and action.value != best_action.value:
                    break
        if best_action != PositionAction.UNKNOWN:
            break

    # 2. 标的提取
    symbol_raw: str | None = None
    normalized_symbol: str | None = None

    # 先试完整 ts_code 模式
    ts_match = _TS_CODE_RE.search(cleaned)
    if ts_match:
        symbol_raw = ts_match.group(0)
        normalized_symbol = _normalize_to_ts_code(symbol_raw)
    else:
        # 再试 6 位代码
        code_match = _CODE_RE.search(cleaned)
        if code_match:
            symbol_raw = code_match.group(1)
            normalized_symbol = _normalize_to_ts_code(symbol_raw)
        else:
            # 最后试中文名称（取第一个非动词的中文词组）
            name_match = _NAME_RE.search(cleaned)
            if name_match:
                symbol_raw = name_match.group(1)
                normalized_symbol = None  # 名称不能直接转代码

    # 3. 数量提取
    quantity: float | None = None
    for pattern, extractor in _QUANTITY_PATTERNS:
        qm = pattern.search(cleaned)
        if qm:
            if extractor:
                quantity = extractor(qm)
            else:
                # 中文数字模式暂不做转换，标记有数量但不解析
                quantity = None
            break

    # 4. 价格提取
    price: float | None = None
    for pattern, extractor in _PRICE_PATTERNS:
        pm = pattern.search(cleaned)
        if pm:
            price = extractor(pm)
            break

    # 5. 置信度微调
    confidence = best_confidence
    if normalized_symbol and confidence > 0:
        confidence = min(1.0, confidence + 0.05)  # 识别到代码，加一点信心
    if quantity is not None and confidence > 0:
        confidence = min(1.0, confidence + 0.03)  # 识别到数量，再加一点

    return PositionIntent(
        action=best_action,
        symbol=normalized_symbol,
        symbol_raw=symbol_raw,
        quantity=quantity,
        price=price,
        confidence=confidence,
        matched_keywords=best_keywords,
    )


def _normalize_to_ts_code(raw: str) -> str | None:
    """把 6 位代码或 ts_code 标准化为 600519.SH 格式。"""
    value = raw.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        return value
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\d{6}", digits):
        return None
    if digits.startswith(("60", "68", "90")):
        return f"{digits}.SH"
    if digits.startswith(("00", "20", "30")):
        return f"{digits}.SZ"
    if digits.startswith(("43", "83", "87", "88", "92")):
        return f"{digits}.BJ"
    return None


def build_intent_hint(intent: PositionIntent) -> str:
    """根据检测到的意图生成注入 system prompt 的提示文本。

    当 confidence < 0.7 时不生成提示（避免误导 LLM）。
    """
    if not intent or intent.action == PositionAction.UNKNOWN or intent.confidence < 0.7:
        return ""

    action_hints: dict[PositionAction, str] = {
        PositionAction.BUY: (
            "用户意图是 **买入/建仓** 新标的。"
            "应先确认标的存在、获取最新价，再按用户给的数量和成本价录入。"
            "缺失字段（代码/数量/成本价）先追问，不臆造。"
        ),
        PositionAction.SELL: (
            "用户意图是 **卖出** 持仓标的。"
            "必须先 get_portfolio_snapshot 获取该标的当前持仓数量，"
            "再按用户指定数量卖出；若未指定数量则追问。"
            "保存时 action=sell，position_quantity 传本次卖出数量（不是卖出后的剩余数量），"
            "后端负责扣减并把回款记入可用资金；用户说了成交价就填 execution_price。"
        ),
        PositionAction.CLOSE: (
            "用户意图是 **清仓** 该标的（全部卖出/退出）。"
            "必须先 get_portfolio_snapshot 确认持仓存在，"
            "然后以 action=close、position_quantity=0 写入（系统自动转 closed，并按全部持仓数量把回款记入可用资金；"
            "用户说了成交价就填 execution_price）。"
            "这是不可逆操作，必须复述确认后才以 confirmed=true 写入。"
        ),
        PositionAction.ADD: (
            "用户意图是 **加仓/补仓** 现有持仓标的。"
            "必须先 get_portfolio_snapshot 获取该标的当前 quantity，"
            "保存时 action=add，position_quantity 传本次加仓数量（不是加仓后的总数），cost_price 传本次成交价；"
            "后端负责累加数量、加权成本并从可用资金扣款。"
        ),
        PositionAction.REDUCE: (
            "用户意图是 **减仓** 现有持仓标的（卖出部分）。"
            "必须先 get_portfolio_snapshot 获取该标的当前 quantity，"
            "保存时 action=reduce，position_quantity 传本次减仓数量（不是减仓后的剩余数量），"
            "后端负责扣减并把回款记入可用资金，减到 0 自动转 closed；用户说了成交价就填 execution_price。"
        ),
        PositionAction.UPDATE_FIELD: (
            "用户意图是 **修改持仓字段**（止损/止盈/逻辑说明/仓位比例）。"
            "应使用 update_position_field 工具做字段级微调，不需要传整仓 rows。"
        ),
        PositionAction.QUERY: (
            "用户意图是 **查看持仓**。"
            "应调用 get_portfolio_snapshot 或 get_my_universe 读取当前持仓和自选池，"
            "不需要调用写工具。"
        ),
    }

    hint = action_hints.get(intent.action, "")
    if not hint:
        return ""

    parts: list[str] = [f"\n\n【系统意图提示 — 置信度 {intent.confidence:.0%}】", hint]
    if intent.symbol:
        parts.append(f"已识别标的代码: {intent.symbol}")
    elif intent.symbol_raw:
        parts.append(f"已识别标的名称（需 resolve_stock 确认）: {intent.symbol_raw}")
    if intent.quantity is not None:
        parts.append(f"已识别数量: {intent.quantity:g} 股")
    if intent.price is not None:
        parts.append(f"已识别价格: {intent.price:g} 元")
    if intent.matched_keywords:
        parts.append(f"匹配关键词: {'、'.join(intent.matched_keywords)}")

    return "\n".join(parts)


def intent_summary(intent: PositionIntent) -> dict[str, Any]:
    """返回意图的结构化摘要（供前端展示或日志记录）。"""
    return {
        "action": intent.action.value,
        "symbol": intent.symbol,
        "symbol_raw": intent.symbol_raw,
        "quantity": intent.quantity,
        "price": intent.price,
        "confidence": round(intent.confidence, 3),
        "matched_keywords": intent.matched_keywords,
    }
