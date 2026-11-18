"""技术面选股器接口：筛选 / 全市场同步 / AI 解析 / AI 解读。

统一用 core.response 信封；鉴权用 require_permission：筛选与同步是只读研究
能力（data:read），AI 解析/解读消耗 LLM（agent:use）。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.market import market_sync
from backend.app.quant import screener_ai, screener_service

router = APIRouter(prefix="/api/screener", tags=["screener"])


class ScoreWeightsIn(BaseModel):
    value: float = Field(default=40, ge=0, le=100, description="估值规模（低 PE、小市值）")
    momentum: float = Field(default=40, ge=0, le=100, description="当日涨跌幅")
    liquidity: float = Field(default=20, ge=0, le=100, description="量比与换手率")


class ScreenerQueryIn(BaseModel):
    industry: str | None = Field(default=None, min_length=1, max_length=120, description="TuShare 股票基础行业，精确匹配")
    market: str | None = Field(default=None, max_length=24, description="市场/交易所过滤")
    stock_type: str | None = Field(default="stock", max_length=24, description="标的类型，默认仅股票")
    price_min: float | None = Field(default=None, ge=0, description="最新价下限（含）")
    price_max: float | None = Field(default=None, ge=0, description="最新价上限（含）")
    pct_chg_min: float | None = Field(default=None, description="当日涨跌幅下限 %（含）")
    pct_chg_max: float | None = Field(default=None, description="当日涨跌幅上限 %（含）")
    keyword: str | None = Field(default=None, max_length=32, description="代码/名称模糊匹配")
    volume_ratio_min: float | None = Field(default=None, gt=0, description="量比下限（官方量比，daily_basic）")
    turnover_rate_min: float | None = Field(default=None, ge=0, description="换手率下限 %（含）")
    turnover_rate_max: float | None = Field(default=None, ge=0, description="换手率上限 %（含）")
    total_mv_min: float | None = Field(default=None, ge=0, description="总市值下限（亿元，含）")
    total_mv_max: float | None = Field(default=None, ge=0, description="总市值上限（亿元，含）")
    pe_min: float | None = Field(default=None, description="市盈率 TTM 下限（含）")
    pe_max: float | None = Field(default=None, description="市盈率 TTM 上限（含）")
    above_ma: int | None = Field(default=None, description="收盘须站上该周期均线（仅 5/10/20）")
    factor_name: str | None = Field(default=None, max_length=64, description="TuShare 官方因子名（如 roe_ttm），按最新缓存截面过滤")
    factor_min: float | None = Field(default=None, description="因子值下限（含；因子值可为负）")
    factor_max: float | None = Field(default=None, description="因子值上限（含；因子值可为负）")
    limit: int = Field(default=50, ge=1, le=100, description="返回条数上限")
    sort_by: Literal["score_desc", "pct_desc", "turnover_desc", "mv_asc", "pe_asc"] = Field(
        default="score_desc", description="全部命中股票的排序方式，items 取排序后的前 limit 只",
    )
    score_weights: ScoreWeightsIn | None = Field(default=None, description="综合分三维权重，缺省 40/40/20")


@router.post("/run")
def screener_run(
    payload: ScreenerQueryIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        result = screener_service.run_screener(
            market=payload.market,
            industry=payload.industry,
            stock_type=payload.stock_type,
            price_min=payload.price_min,
            price_max=payload.price_max,
            pct_chg_min=payload.pct_chg_min,
            pct_chg_max=payload.pct_chg_max,
            keyword=payload.keyword,
            volume_ratio_min=payload.volume_ratio_min,
            turnover_rate_min=payload.turnover_rate_min,
            turnover_rate_max=payload.turnover_rate_max,
            total_mv_min=payload.total_mv_min,
            total_mv_max=payload.total_mv_max,
            pe_min=payload.pe_min,
            pe_max=payload.pe_max,
            above_ma=payload.above_ma,
            factor_name=payload.factor_name,
            factor_min=payload.factor_min,
            factor_max=payload.factor_max,
            limit=payload.limit,
            sort_by=payload.sort_by,
            score_weights=payload.score_weights.model_dump() if payload.score_weights else None,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)


class SyncIn(BaseModel):
    days: int = Field(default=20, ge=5, le=260, description="同步最近 N 个交易日")


@router.post("/sync")
def screener_sync(
    payload: SyncIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    """全市场增量同步（幂等）；上游错误原文透出，便于定位 TuShare 积分/权限问题。"""
    try:
        stats = market_sync.sync_market(payload.days)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="upstream_error"))
    return ok(stats)


@router.get("/sync-status")
def screener_sync_status(
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok(market_sync.sync_status())


class ParseIn(BaseModel):
    text: str = Field(max_length=500, description="一句话自然语言选股条件")


class PromptRecordIn(BaseModel):
    text: str = Field(max_length=500, description="已知结构化条件对应的提示词")
    query: ScreenerQueryIn
    parse_summary: str = Field(default="", max_length=1000)


@router.post("/parse")
def screener_parse(
    payload: ParseIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = screener_ai.parse_query(payload.text)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="llm_error"))
    history = screener_service.record_prompt_history(
        context.user.id,
        payload.text,
        parsed_query=result.get("query") or {},
        parse_summary=result.get("summary") or "",
    )
    return ok({**result, "history_id": history["id"]})


@router.post("/prompt-history/record")
def screener_prompt_record(
    payload: PromptRecordIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """Record a template/history reuse without paying for another LLM parse."""
    query = payload.query.model_dump(exclude_none=True, exclude_unset=True)
    history = screener_service.record_prompt_history(
        context.user.id,
        payload.text,
        parsed_query=query,
        parse_summary=payload.parse_summary or screener_ai.summarize_query(query),
    )
    return ok(history)


@router.get("/prompt-history")
def screener_prompt_history(
    limit: int = Query(default=50, ge=1, le=100),
    search: str | None = Query(default=None, max_length=120),
    favorite_only: bool = Query(default=False),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok(
        screener_service.list_prompt_history(
            context.user.id,
            limit=limit,
            search=search,
            favorite_only=favorite_only,
        )
    )


class PromptFavoriteIn(BaseModel):
    is_favorite: bool


@router.patch("/prompt-history/{history_id}/favorite")
def screener_prompt_favorite(
    history_id: int,
    payload: PromptFavoriteIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        item = screener_service.set_prompt_favorite(
            context.user.id, history_id, payload.is_favorite
        )
    except ValueError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    return ok(item)


class PromptResultIn(BaseModel):
    result_count: int = Field(ge=0)


@router.patch("/prompt-history/{history_id}/result")
def screener_prompt_result(
    history_id: int,
    payload: PromptResultIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        item = screener_service.update_prompt_result_count(
            context.user.id, history_id, payload.result_count
        )
    except ValueError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    return ok(item)


class SavedStrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    query: ScreenerQueryIn
    last_result_count: int | None = Field(default=None, ge=0)


@router.get("/strategies")
def screener_saved_strategies(
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok({"items": screener_service.list_saved_strategies(context.user.id)})


@router.post("/strategies")
def screener_save_strategy(
    payload: SavedStrategyIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        item = screener_service.save_strategy(
            context.user.id,
            payload.name,
            payload.query.model_dump(exclude_none=True),
            last_result_count=payload.last_result_count,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(item)


@router.patch("/strategies/{strategy_id}/result")
def screener_saved_strategy_result(
    strategy_id: int,
    payload: PromptResultIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        item = screener_service.update_saved_strategy_result(
            context.user.id, strategy_id, payload.result_count
        )
    except ValueError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    return ok(item)


@router.delete("/strategies/{strategy_id}")
def screener_delete_strategy(
    strategy_id: int,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        screener_service.delete_saved_strategy(context.user.id, strategy_id)
    except ValueError as exc:
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    return ok({"deleted": True, "id": strategy_id})


class InterpretIn(BaseModel):
    query: dict = Field(default_factory=dict, description="本次筛选条件")
    universe: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    items: list[dict] = Field(default_factory=list, description="命中股票明细（前端截前 15）")


@router.post("/interpret")
def screener_interpret(
    payload: InterpretIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = screener_ai.interpret_result(
            payload.query, payload.universe, payload.total, payload.items
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="llm_error"))
    return ok(result)


@router.get("/templates")
def screener_templates(
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    """读取该用户最近一次智能模板推荐；从未生成过则返回空列表。"""
    rec = screener_service.load_template_recommendation(context.user.id)
    if rec is None:
        return ok(
            {"templates": [], "basis": "", "position_count": 0, "model": None, "generated_at": None}
        )
    return ok(rec)


@router.post("/templates")
def screener_templates_refresh(
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    """Generate ten factual next-step strategies and bound each candidate pool."""
    try:
        facts = screener_service.position_facts(context.user.id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="no_positions"))
    templates = []
    for tpl in screener_service.build_progression_templates(facts):
        try:
            refined = screener_service.refine_progression_template(
                tpl,
                screener_service.count_screener_candidates,
            )
        except ValueError:
            continue
        if refined["universe"] <= 0:
            continue
        refined["prompt"] = screener_ai.summarize_query(refined["query"]).removeprefix("已解析：")
        templates.append(refined)
    payload = {
        "templates": templates[:10],
        "basis": screener_service.position_profile_summary(facts),
        "position_count": facts["position_count"],
        "model": "持仓画像规则引擎-v2",
    }
    generated_at = screener_service.save_template_recommendation(context.user.id, payload)
    return ok({**payload, "generated_at": generated_at})
