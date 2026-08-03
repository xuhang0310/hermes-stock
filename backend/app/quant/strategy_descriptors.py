"""策略登记 — 单一事实源。

所有"信号类"策略的元数据（人话名 / 科普 / 场景 / 风险 / 默认参数 / 默认股票池）
集中写在 ``STRATEGY_DESCRIPTORS``，以下各处全部派生于它：

- 聊天工具 ``run_signal_backtest`` 的 strategy enum（skills/strategy_backtest.py）
- 聊天工具 ``list_backtest_strategies`` 的列表（backtest_runner.list_signal_engines）
- 报告页中文名（backtest_runner._enrich_report）
- 策略库卡片数据源（GET /api/backtests/strategies）

新增策略时：在此登记 + 在 ``SIGNAL_ENGINE_REGISTRY`` 注册引擎即可，
enum / 卡片 / 中文名自动出现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 横截面动量默认股票池：24 只流动性较好的大盘 A 股示例
_MOMENTUM_DEFAULT_POOL = [
    "600519.SH",  # 贵州茅台
    "000858.SZ",  # 五粮液
    "601318.SH",  # 中国平安
    "600036.SH",  # 招商银行
    "000001.SZ",  # 平安银行
    "600900.SH",  # 长江电力
    "601899.SH",  # 紫金矿业
    "600030.SH",  # 中信证券
    "000333.SZ",  # 美的集团
    "000651.SZ",  # 格力电器
    "600276.SH",  # 恒瑞医药
    "600887.SH",  # 伊利股份
    "601012.SH",  # 隆基绿能
    "300750.SZ",  # 宁德时代
    "002594.SZ",  # 比亚迪
    "002415.SZ",  # 海康威视
    "600809.SH",  # 山西汾酒
    "000568.SZ",  # 泸州老窖
    "601888.SH",  # 中国中免
    "600585.SH",  # 海螺水泥
    "600104.SH",  # 上汽集团
    "601668.SH",  # 中国建筑
    "000725.SZ",  # 京东方A
    "002475.SZ",  # 立讯精密
]

# 单票策略的示例标的
_SINGLE_DEFAULT_SYMBOL = ["000001.SZ"]

STRATEGY_DESCRIPTORS: List[Dict[str, Any]] = [
    {
        "code": "macd",
        "name": "MACD 金叉死叉",
        "description": "趋势跟踪：趋势走强时持有，转弱时离场",
        "explain": (
            "MACD 是衡量一只股票趋势强弱的指标：短期趋势线向上穿过长期趋势线叫"
            "「金叉」，买入；向下穿过叫「死叉」，卖出。通俗讲就是顺势而为——涨出"
            "势头就买，势头没了就卖。适合捕捉一段像样的趋势行情，缺点是在来回震荡"
            "的横盘市里容易被反复打脸。"
        ),
        "scenario": "趋势明确的单边行情，适合单只股票",
        "risk_level": "medium",
        "kind": "single",
        "default_symbols": _SINGLE_DEFAULT_SYMBOL,
        "default_params": {"fast": 12, "slow": 26, "signal": 9},
        "param_schema": {"fast": "快线周期", "slow": "慢线周期", "signal": "信号线周期"},
        "chart": {
            "overlays": [],
            "osc": [
                {"key": "macd", "label": "DIF", "color": "#eabf63"},
                {"key": "signal_line", "label": "DEA", "color": "#49b8d1"},
                {"key": "histogram", "label": "MACD柱", "color": "#8f979d", "histogram": True},
            ],
        },
    },
    {
        "code": "ma_cross",
        "name": "双均线趋势",
        "description": "短期均线上穿长期均线买入，下穿卖出",
        "explain": (
            "均线就是最近一段时间的收盘均价。5 日线代表短期情绪，20 日线代表中期"
            "情绪：短期线向上穿过中期线（金叉）说明近期买盘变强，跟随买入；向下"
            "穿过（死叉）则离场。通俗讲就是快线超过慢线就买、跌破就卖。适合有趋势"
            "的行情，横盘时会两头挨打。"
        ),
        "scenario": "趋势行情，适合单只股票",
        "risk_level": "medium",
        "kind": "single",
        "default_symbols": _SINGLE_DEFAULT_SYMBOL,
        "default_params": {"short_period": 5, "long_period": 20},
        "param_schema": {"short_period": "短期均线天数", "long_period": "长期均线天数"},
        "chart": {
            "overlays": [
                {"key": "ma_short", "label": "MA{short_period}", "color": "#eabf63"},
                {"key": "ma_long", "label": "MA{long_period}", "color": "#49b8d1"},
            ],
            "osc": [],
        },
    },
    {
        "code": "bollinger",
        "name": "布林带回归",
        "description": "跌到下轨超卖时买入，涨回上轨时离场",
        "explain": (
            "布林带是围绕均价画出的一条通道：价格涨过上轨算「过热」，跌破下轨算"
            "「超卖」。这个策略赌均值回归——价格跌到下轨买入（跌多了会反弹），"
            "涨回上轨卖出。通俗讲就是价格像钟摆，摆过头了就会被拉回来。适合震荡"
            "行情，单边大涨大跌时容易卖飞或抄底被套。"
        ),
        "scenario": "横盘震荡、均值回归行情，适合单只股票",
        "risk_level": "medium",
        "kind": "single",
        "default_symbols": _SINGLE_DEFAULT_SYMBOL,
        "default_params": {"period": 20, "num_std": 2.0},
        "param_schema": {"period": "均线周期", "num_std": "通道宽度（标准差倍数）"},
        "chart": {
            "overlays": [
                {"key": "boll_upper", "label": "上轨", "color": "#ff6b70"},
                {"key": "boll_mid", "label": "中轨", "color": "#eabf63"},
                {"key": "boll_lower", "label": "下轨", "color": "#70a5ff"},
            ],
            "osc": [],
        },
    },
    {
        "code": "rsi",
        "name": "RSI 超买超卖",
        "description": "超卖恐慌时买入，超买过热时卖出",
        "explain": (
            "RSI（相对强弱指标）给近期的涨跌热度打分，范围 0~100：一般高于 70 叫"
            "超买（涨太猛），低于 30 叫超卖（跌太狠）。这个策略在低于 30 时买入、"
            "高于 70 时卖出。通俗讲就是涨太多的不追、跌太狠的捡反弹。适合震荡市，"
            "强趋势行情里容易过早卖出。"
        ),
        "scenario": "震荡回归行情，适合单只股票",
        "risk_level": "medium",
        "kind": "single",
        "default_symbols": _SINGLE_DEFAULT_SYMBOL,
        "default_params": {"period": 14, "oversold": 30.0, "overbought": 70.0},
        "param_schema": {"period": "RSI 周期", "oversold": "超卖阈值", "overbought": "超买阈值"},
        "chart": {
            "overlays": [],
            "osc": [
                {"key": "rsi", "label": "RSI{period}", "color": "#4dd0be"},
                {"key": "overbought_line", "label": "超买线", "color": "#ff6b70"},
                {"key": "oversold_line", "label": "超卖线", "color": "#9cd36a"},
            ],
        },
    },
    {
        "code": "momentum",
        "name": "强者恒强（动量选股）",
        "description": "每天买入过去一段时间涨得最好的一批股票",
        "explain": (
            "这在学术上叫「横截面动量」，是著名的市场异象之一：过去一段时间涨得"
            "多的股票，短期内往往还会继续强。策略每天把股票池里所有股票按最近 "
            "N 天涨幅排名，选出最强的 M 只平均买入，第二天根据新排名自动换仓。"
            "通俗讲就是不自己挑股，让市场替你选出最强的那批。风险：市场风格切换"
            "时（强者补跌、弱者补涨）回撤可能较大。"
        ),
        "scenario": "有明显领涨板块的市场，一篮子股票横向比较",
        "risk_level": "high",
        "kind": "cross_sectional",
        "default_symbols": _MOMENTUM_DEFAULT_POOL,
        "default_params": {"lookback": 20, "top_n": 5},
        "param_schema": {"lookback": "回看排名天数（涨幅窗口）", "top_n": "持有最强的前 N 只"},
        "chart": {
            "overlays": [],
            "osc": [
                {"key": "momentum", "label": "动量·{lookback}日（首标的视角）", "color": "#c78cff"},
            ],
        },
    },
    {
        "code": "turtle",
        "name": "海龟趋势跟踪",
        "description": "经典突破追趋势：创新高买入、跌破通道离场，自带加仓和止损",
        "explain": (
            "海龟交易法则是最经典的趋势策略之一，1983 年由传奇交易员理查德·丹尼斯"
            "用一群零基础学员验证成名。规则很纪律：股价创出最近 20 日新高就买入"
            "（突破入场）；趋势延续、每再涨一段（按日常波动幅度 ATR 的一半）就加"
            "一次仓，最多加到 4 次；跌破买入均价两个波动幅度（2N 止损）无条件认错；"
            "或者股价跌回最近 10 日最低点以下（通道离场）全部卖出。通俗讲就是"
            "「追创新高、顺趋势加码、破位就跑」，风险管理是它的精髓。适合走出大"
            "趋势的个股；横盘震荡时会被反复止损打脸。"
        ),
        "scenario": "趋势明显的个股，看重纪律化风险管理",
        "risk_level": "medium",
        "kind": "single",
        "default_symbols": _SINGLE_DEFAULT_SYMBOL,
        "default_params": {
            "entry_period": 20,
            "exit_period": 10,
            "atr_period": 20,
            "max_units": 4,
            "unit_weight": 0.25,
            "stop_atr_mult": 2.0,
            "add_atr_mult": 0.5,
        },
        "param_schema": {
            "entry_period": "入场突破窗口（前 N 日高点）",
            "exit_period": "离场通道窗口（前 N 日低点）",
            "atr_period": "波动幅度 ATR 的计算窗口",
            "max_units": "最多加仓几次",
            "unit_weight": "每次建仓占多少仓位",
            "stop_atr_mult": "止损距离（几个波动幅度）",
            "add_atr_mult": "加仓间距（几个波动幅度）",
        },
        "chart": {
            "overlays": [
                {"key": "entry_channel", "label": "唐奇安上轨·入场", "color": "#9cd36a"},
                {"key": "exit_channel", "label": "唐奇安下轨·离场", "color": "#70a5ff"},
                {"key": "stop_line", "label": "2N止损线", "color": "#ff6b70"},
            ],
            "osc": [
                {"key": "atr", "label": "ATR·波动幅度", "color": "#eabf63"},
            ],
        },
    },
]


def get_descriptor(code: str) -> Optional[Dict[str, Any]]:
    """按 code 取策略登记项，未登记返回 None。"""
    for descriptor in STRATEGY_DESCRIPTORS:
        if descriptor["code"] == code:
            return descriptor
    return None


# 暴露给前端卡片的字段子集（不含 param_schema 等内部字段）
_PUBLIC_FIELDS = (
    "code",
    "name",
    "description",
    "explain",
    "scenario",
    "risk_level",
    "kind",
    "default_symbols",
    "default_params",
)


def descriptor_public() -> List[Dict[str, Any]]:
    """策略库卡片数据源：只含前端需要的字段。"""
    return [{key: descriptor[key] for key in _PUBLIC_FIELDS} for descriptor in STRATEGY_DESCRIPTORS]


def strategy_enum() -> List[str]:
    """聊天工具 run_signal_backtest 的 strategy enum。"""
    return [descriptor["code"] for descriptor in STRATEGY_DESCRIPTORS]


def code_to_name() -> Dict[str, str]:
    """code → 人话名 映射。"""
    return {descriptor["code"]: descriptor["name"] for descriptor in STRATEGY_DESCRIPTORS}


def report_name(code: str) -> str:
    """报告页展示的策略名（登记内 → 「人话名 + 回测」，未登记 → 原 code）。"""
    descriptor = get_descriptor(code)
    return f"{descriptor['name']}回测" if descriptor else code
