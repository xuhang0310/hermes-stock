"""Pydantic AI v2 试点 —— 持仓录入的结构化输出 + 校验重试。

独立于 position_import.py（旧路径 chat_completion 不动），通过 env
``STOCK_POSITION_AGENT_V2=true`` 开启。失败随时关回旧路径，零风险回退。

收益对照（旧 vs 新）：
- 旧：LLM 返回自由文本 JSON，手写 _extract_json 容错提取，失败即报错
- 新：result_type=PositionParseResult 约束输出，Pydantic 校验，失败自动
  反思重试（retries=2）；多模态图片经 BinaryContent 透传

真实视觉 LLM 验证需 ``STOCK_LLM_*`` 配一个支持视觉的模型并开关本 env，由
用户自行确认行为一致后保留。
"""
from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field


def is_v2_enabled() -> bool:
    return os.getenv("STOCK_POSITION_AGENT_V2", "false").lower() == "true"


# ---- 结构化输出模型（与旧路径 JSON 契约逐字对齐，便于结果互通后处理）----

class PositionAccountOut(BaseModel):
    broker_name: str | None = None
    account_name: str | None = None
    account_no_masked: str | None = None
    account_type: str | None = None


class PositionRowOut(BaseModel):
    symbol: str = Field(description="A股代码，优先 600519.SH/000001.SZ/430xxx.BJ，6位代码亦可")
    name: str | None = None
    position_quantity: float = Field(description="持仓数量/股份余额")
    available_quantity: float | None = None
    frozen_quantity: float | None = None
    today_buy_quantity: float | None = None
    today_sell_quantity: float | None = None
    cost_price: float | None = None
    cost_amount: float | None = None
    latest_price: float | None = None
    market_value: float | None = None
    floating_pnl: float | None = None
    floating_pnl_pct: float | None = None
    day_pnl: float | None = None
    position_pct: float | None = None
    confidence: float | None = None


class PositionParseResult(BaseModel):
    """LLM 持仓录入的结构化输出契约。校验失败时 v2 自动反思重试。"""
    intent: Literal["greeting", "add_position", "clarify", "unsupported"]
    should_save: bool
    reply: str = Field(description="给用户看的中文回复")
    account: PositionAccountOut | None = None
    as_of_date: str | None = None
    rows: list[PositionRowOut] = Field(default_factory=list)


_SYSTEM_PROMPT = """你是 SixRabbits Stock 的持仓录入 Agent。不是普通 OCR，而是对话式持仓录入助手。
只输出结构化结果（由系统约束），不要输出解释文字。
工作规则：
1. 用户问候/测试/闲聊（你好/在吗/你是谁）→ intent=greeting, should_save=false, rows=[]
2. 想添加但字段不足（只说"帮我添加贵州茅台"）→ intent=clarify, should_save=false, rows=[]，reply 追问代码/数量/成本
3. 能识别至少一条持仓明细 → intent=add_position, should_save=true，填充 rows
4. 数值字段纯数字，不带逗号/元/股/%
5. 不确定字段留空，不要猜
6. 图片场景只识别持仓明细行，不要把资产汇总/按钮/广告识别成持仓
7. A股代码：6开头.SH，0/3开头.SZ，4/8开头.BJ"""


def parse_with_pydantic_ai(
    *,
    instruction: str,
    image_bytes: bytes | None,
    mime_type: str,
    llm_config: dict,
) -> dict[str, Any]:
    """用 Pydantic AI v2 Agent 跑结构化提取。返回与旧路径同构的 dict。"""
    from pydantic_ai import Agent
    from pydantic_ai.messages import BinaryContent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        base_url=llm_config["base_url"], api_key=llm_config["api_key"]
    )
    model = OpenAIChatModel(llm_config["model"], provider=provider)
    agent = Agent(model, result_type=PositionParseResult, retries=2, system_prompt=_SYSTEM_PROMPT)

    user_content: list[Any] = [{"type": "text", "text": instruction}]
    if image_bytes:
        user_content.append(BinaryContent(data=image_bytes, media_type=mime_type or "image/png"))

    result = agent.run_sync(user_content)
    data = result.output.model_dump()
    # 与旧路径字段名对齐：rows 已是 list[dict]，account 已是 dict|None
    return {
        "model": {
            "id": llm_config.get("id"),
            "display_name": llm_config.get("display_name"),
            "model_name": llm_config.get("model"),
            "provider_name": llm_config.get("provider_name"),
        },
        "image_sha256": None,
        "raw_text": None,
        "intent": data["intent"],
        "should_save": data["should_save"],
        "reply": data["reply"],
        "account": data["account"],
        "as_of_date": data.get("as_of_date"),
        "rows": data["rows"],
    }
