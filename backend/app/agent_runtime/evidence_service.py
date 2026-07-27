from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    ResearchClaim,
    ResearchCompletionAudit,
    ResearchEvidence,
    ResearchGoal,
    ResearchGoalCriterion,
)
from backend.app.skills.manifests import SkillManifest
from backend.app.skills.manifests import CORE_SKILL_MANIFESTS, SkillCriterion


_A_SHARE_SYMBOL_PATTERN = re.compile(r"(?<!\d)(\d{6})(?:\.(?:SH|SZ|BJ))?(?!\d)", re.I)


def _build_research_plan(
    objective: str,
    selected: list[SkillManifest],
) -> dict[str, Any]:
    selected_names = [item.name for item in selected]
    symbols = list(
        dict.fromkeys(match.group(0).upper() for match in _A_SHARE_SYMBOL_PATTERN.finditer(objective))
    )
    if "position-decision-support" in selected_names:
        task = "position_decision"
    elif "company-deep-research" in selected_names:
        task = "company_deep_research"
    else:
        task = "evidence_driven_research"

    criteria: list[dict[str, Any]] = []
    sections: list[str] = []
    seen_criteria: set[str] = set()
    for manifest in selected:
        for criterion in manifest.criteria:
            if criterion.code in seen_criteria:
                continue
            seen_criteria.add(criterion.code)
            criteria.append(
                {
                    "code": criterion.code,
                    "description": criterion.description,
                    "required": criterion.required,
                    "min_evidence": criterion.min_evidence,
                    "allowed_tools": list(criterion.tool_names),
                    "max_age_days": criterion.max_age_days,
                }
            )
        for section in manifest.output_contract:
            if section not in sections:
                sections.append(section)

    return {
        "task": task,
        "symbols": symbols,
        "selected_skills": selected_names,
        "required": [item["code"] for item in criteria if item["required"]],
        "criteria": criteria,
        "report_sections": sections,
        "execution": {
            "standard_bundle_first": task in {"company_deep_research", "position_decision"},
            "react_gap_fill": True,
            "completion_audit": True,
            "max_audit_retries": 2,
            "stop_when_no_new_evidence": True,
        },
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _provider_for(tool_name: str, result: dict[str, Any]) -> str | None:
    for key in ("provider", "source_provider", "data_source", "source"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    if tool_name.startswith(("get_tushare", "get_financial", "get_income", "get_margin")):
        return "tushare"
    if tool_name.startswith("search_"):
        return "search"
    if tool_name.startswith("ws_"):
        return "workspace"
    if tool_name in {"dispatch_agent_tasks", "list_delegatable_agents"}:
        return "agent_team"
    return "hermes"


def _source_ref(result: dict[str, Any]) -> str | None:
    for key in ("url", "source_url", "path", "symbol", "ts_code", "artifact_id"):
        value = result.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:500]
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
        return str(artifacts[0].get("path") or artifacts[0].get("artifact_id") or "")[:500] or None
    return None


def _criterion_definition(code: str) -> SkillCriterion | None:
    for manifest in CORE_SKILL_MANIFESTS:
        for criterion in manifest.criteria:
            if criterion.code == code:
                return criterion
    return None


def _first_nonempty(result: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = result.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _data_as_of(result: dict[str, Any]) -> str | None:
    value = _first_nonempty(
        result,
        "data_as_of",
        "trade_date",
        "end_trade_date",
        "report_date",
        "period",
        "end_date",
        "fetched_at",
        "updated_at",
    )
    if value is None and isinstance(result.get("latest"), dict):
        value = _first_nonempty(
            result["latest"], "trade_date", "data_as_of", "report_date", "updated_at"
        )
    if value is None:
        for key in ("bars", "rows", "results", "hits", "positions", "watchlist"):
            rows = result.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
                value = _first_nonempty(
                    rows[-1],
                    "trade_date",
                    "data_as_of",
                    "report_date",
                    "period",
                    "published_at",
                    "ann_date",
                    "date",
                    "updated_at",
                )
                if value is not None:
                    break
    return str(value)[:80] if value is not None else None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.strip()[:10].replace("/", "-")
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(normalized.replace("-", "") if pattern == "%Y%m%d" else normalized, pattern).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _symbol_universe(result: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("symbol", "ts_code"):
        value = result.get(key)
        if value:
            values.append(str(value))
    for key in ("symbols", "symbol_universe"):
        items = result.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, dict) and item.get("symbol"):
                    values.append(str(item["symbol"]))
    return list(dict.fromkeys(values))[:100]


def _source_uri(result: dict[str, Any]) -> str | None:
    direct = _first_nonempty(result, "source_uri", "url", "source_url")
    if direct:
        return str(direct)[:1000]
    for key in ("results", "hits"):
        rows = result.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    value = _first_nonempty(row, "url", "source_uri", "source_url", "document_id")
                    if value:
                        return str(value)[:1000]
    return None


def _assess_result(
    criterion: SkillCriterion | None,
    tool_name: str,
    result: dict[str, Any],
    success: bool,
) -> dict[str, Any]:
    profile = criterion.validation_profile if criterion else "substantive"
    reasons: list[str] = []
    if not success:
        reasons.append("tool execution did not succeed")
    if result.get("error"):
        reasons.append(f"tool returned error: {str(result['error'])[:200]}")

    valid_content = False
    profile_as_of: str | None = None
    if profile == "market_data":
        valid_content = result.get("price") is not None or bool(result.get("bars"))
        if result.get("stale"):
            reasons.append("market result is marked stale")
    elif profile == "fundamental":
        valid_content = any(
            (
                isinstance(result.get("financial"), dict) and bool(result["financial"]),
                bool(result.get("rows")),
                result.get("pe") is not None,
                result.get("pb") is not None,
                result.get("total_mv") is not None,
            )
        )
    elif profile == "sourced_list":
        rows = result.get("results")
        valid_content = bool(
            isinstance(rows, list)
            and any(
                isinstance(row, dict)
                and row.get("title")
                and (row.get("url") or row.get("source") or row.get("provider"))
                for row in rows
            )
        )
    elif profile == "technical":
        valid_content = bool(
            result.get("bars")
            or result.get("trend")
            or result.get("pattern")
            or result.get("ma")
            or result.get("ma5") is not None
            or result.get("latest")
        )
    elif profile == "capital_flow":
        valid_content = bool(
            result.get("rows")
            or result.get("main_net_amount") is not None
            or result.get("winner_rate") is not None
            or result.get("data")
        )
    elif profile == "research_context":
        valid_content = bool(
            result.get("has_context")
            or result.get("bars")
            or result.get("hits")
            or result.get("results")
            or result.get("evidence")
            or result.get("reports")
        )
    elif profile == "positions":
        valid_content = isinstance(result.get("positions"), list) or bool(
            result.get("stop_status") or result.get("items")
        )
    elif profile == "decision_position":
        position = result.get("position")
        valid_content = bool(
            isinstance(position, dict)
            and position.get("quantity") is not None
            and position.get("cost_price") is not None
            and not result.get("account_ambiguous")
        )
        if result.get("account_ambiguous"):
            reasons.append("same symbol exists in multiple accounts; account selection is required")
    elif profile == "decision_market":
        market = result.get("market")
        valid_content = bool(
            isinstance(market, dict)
            and market.get("price") is not None
            and market.get("trade_date")
        )
        if isinstance(market, dict) and market.get("stale"):
            reasons.append("decision market result is marked stale")
    elif profile == "decision_technical":
        technical = result.get("technical")
        valid_content = bool(
            isinstance(technical, dict)
            and int(technical.get("bar_count") or 0) >= 20
            and technical.get("end_trade_date")
            and isinstance(technical.get("recent_bars"), list)
        )
        if isinstance(technical, dict):
            profile_as_of = technical.get("end_trade_date")
    elif profile == "decision_fundamental":
        research = result.get("research")
        fundamental = (
            research.get("fundamental")
            if isinstance(research, dict) and isinstance(research.get("fundamental"), dict)
            else {}
        )
        income_trend = (
            research.get("income_trend")
            if isinstance(research, dict) and isinstance(research.get("income_trend"), list)
            else []
        )
        valid_content = bool(
            fundamental.get("industry")
            and (
                fundamental.get("latest_financial")
                or income_trend
            )
        )
        profile_as_of = fundamental.get("data_as_of") or (
            income_trend[0].get("end_date")
            if income_trend and isinstance(income_trend[0], dict)
            else None
        )
    elif profile == "decision_capital":
        research = result.get("research")
        capital = (
            research.get("capital")
            if isinstance(research, dict) and isinstance(research.get("capital"), dict)
            else {}
        )
        valid_content = bool(
            capital.get("flow_recent")
            or capital.get("margin_recent")
            or capital.get("chip")
        )
        profile_as_of = capital.get("data_as_of")
    elif profile == "company_market":
        market = result.get("market") if isinstance(result.get("market"), dict) else result
        timeline = market.get("timeline") if isinstance(market, dict) else None
        valid_content = bool(
            isinstance(market, dict)
            and market.get("price") is not None
            and market.get("trade_date")
            and isinstance(timeline, list)
        )
        profile_as_of = market.get("trade_date") if isinstance(market, dict) else None
        if isinstance(market, dict) and market.get("stale"):
            reasons.append("company market result is marked stale")
    elif profile == "company_financial":
        financial = result.get("financial") if isinstance(result.get("financial"), dict) else {}
        valid_content = bool(
            financial.get("income_trend")
            and (financial.get("balance_trend") or financial.get("cashflow_trend"))
        )
        profile_as_of = financial.get("data_as_of")
    elif profile == "company_valuation":
        valuation = result.get("valuation") if isinstance(result.get("valuation"), dict) else result
        valid_content = bool(
            isinstance(valuation, dict)
            and any(
                valuation.get(key) is not None
                for key in ("pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv")
            )
        )
        profile_as_of = valuation.get("data_as_of") if isinstance(valuation, dict) else None
    elif profile == "company_events":
        events = result.get("events") if isinstance(result.get("events"), dict) else {}
        checked = events.get("checked_sources") if isinstance(events, dict) else None
        direct_rows = result.get("rows") or result.get("results")
        valid_content = bool(
            (isinstance(checked, list) and any(item.get("ok") for item in checked if isinstance(item, dict)))
            or (isinstance(direct_rows, list) and direct_rows)
        )
        profile_as_of = events.get("data_as_of") if isinstance(events, dict) else None
    elif profile == "company_technical":
        technical = result.get("technical") if isinstance(result.get("technical"), dict) else {}
        bars = result.get("bars")
        valid_content = bool(
            (
                isinstance(technical, dict)
                and int(technical.get("bar_count") or 0) >= 60
                and technical.get("end_trade_date")
            )
            or (isinstance(bars, list) and len(bars) >= 60)
        )
        profile_as_of = (
            technical.get("end_trade_date")
            if isinstance(technical, dict)
            else None
        )
    elif profile == "company_capital":
        capital = result.get("capital") if isinstance(result.get("capital"), dict) else result
        valid_content = bool(
            isinstance(capital, dict)
            and (
                capital.get("flow_recent")
                or capital.get("margin_recent")
                or capital.get("chip")
                or capital.get("rows")
                or capital.get("main_net_amount") is not None
            )
        )
        profile_as_of = capital.get("data_as_of") if isinstance(capital, dict) else None
    elif profile == "company_counter_evidence":
        counter = (
            result.get("counter_evidence")
            if isinstance(result.get("counter_evidence"), dict)
            else {}
        )
        valid_content = bool(counter.get("facts"))
        profile_as_of = counter.get("data_as_of")
    elif profile == "event_material":
        rows = result.get("rows") or result.get("results")
        valid_content = bool(
            isinstance(rows, list)
            and any(
                isinstance(row, dict)
                and (
                    row.get("ann_title")
                    or row.get("title")
                    or row.get("content")
                )
                and (
                    row.get("ann_date")
                    or row.get("published_at")
                    or row.get("date")
                    or row.get("url")
                    or row.get("source")
                )
                for row in rows
            )
        )
    elif profile == "watchlist":
        valid_content = isinstance(result.get("watchlist"), list) or isinstance(
            result.get("in_user_universe"), bool
        )
    elif profile == "knowledge_hits":
        valid_content = bool(result.get("hits"))
    elif profile == "artifact":
        artifacts = result.get("artifacts")
        valid_content = bool(
            isinstance(artifacts, list)
            and any(
                isinstance(item, dict)
                and item.get("artifact_id")
                and item.get("sha256")
                and item.get("validation_status") == "valid"
                for item in artifacts
            )
        )
    elif profile == "code_execution":
        valid_content = (
            result.get("exit_code") == 0
            and not result.get("timed_out")
            and bool(result.get("stdout") or result.get("artifacts"))
        )
    else:
        meaningful = {
            key: value
            for key, value in result.items()
            if key not in {"source", "provider", "ms"} and value not in (None, "", [], {})
        }
        valid_content = bool(meaningful)

    if not valid_content:
        reasons.append(f"result does not satisfy validation profile {profile}")

    as_of = profile_as_of or _data_as_of(result)
    freshness_status = "not_required"
    if criterion and criterion.max_age_days is not None:
        parsed = _parse_date(as_of)
        if parsed is None:
            freshness_status = "unknown"
            reasons.append("required data date is missing or invalid")
        else:
            age_days = (datetime.utcnow().date() - parsed).days
            if age_days < -1:
                freshness_status = "invalid"
                reasons.append(f"data date is {-age_days} days in the future")
            else:
                freshness_status = "fresh" if age_days <= criterion.max_age_days else "stale"
            if freshness_status == "stale":
                reasons.append(
                    f"data is {age_days} days old; limit is {criterion.max_age_days}"
                )

    verified = bool(not reasons and valid_content)
    return {
        "profile": profile,
        "verified": verified,
        "verification_status": "verified" if verified else "unverified",
        "freshness_status": freshness_status,
        "data_as_of": as_of,
        "confidence": "high" if verified and freshness_status == "fresh" else (
            "medium" if verified else "low"
        ),
        "reasons": reasons,
        "tool_name": tool_name,
    }


def _decision_answer_issues(
    answer: str,
    *,
    selected_skills: set[str],
    verified_tools: set[str],
) -> list[dict[str, Any]]:
    if "position-decision-support" not in selected_skills:
        return []
    text = answer.strip()
    requirements = (
        (
            "decision.answer_summary",
            "回答必须先给出执行摘要或核心结论。",
            ("执行摘要", "核心结论"),
        ),
        (
            "decision.answer_as_of",
            "回答必须明确行情或证据的数据截止日期。",
            ("数据截止", "截至", "交易日", "数据日期"),
        ),
        (
            "decision.answer_scenarios",
            "回答必须给出至少两种条件化情景或方案。",
            ("方案", "情景"),
        ),
        (
            "decision.answer_triggers",
            "回答必须写明方案触发条件。",
            ("触发", "如果", "若", "条件"),
        ),
        (
            "decision.answer_limits",
            "回答必须写明风险、数据缺口或失效条件。",
            ("风险", "数据缺口", "失效条件", "未核验"),
        ),
        (
            "decision.answer_fundamental",
            "回答必须覆盖公司基本面、财务或估值。",
            ("基本面", "财务", "估值"),
        ),
        (
            "decision.answer_technical",
            "回答必须覆盖技术面或量价结构。",
            ("技术", "量价", "均线", "支撑", "阻力"),
        ),
        (
            "decision.answer_capital",
            "回答必须覆盖资金、两融或筹码；无数据时明确未核验。",
            ("资金", "两融", "融资", "筹码", "未核验"),
        ),
        (
            "decision.answer_matrix",
            "回答必须给出多维决策矩阵或评分表。",
            ("决策矩阵", "多维", "评分"),
        ),
    )
    issues = [
        {
            "code": code,
            "description": description,
            "evidence_count": 0,
            "min_evidence": 1,
        }
        for code, description, terms in requirements
        if not any(term in text for term in terms)
    ]

    capital_terms = ("主力", "机构接盘", "资金持续流入", "资金持续流出", "散户接盘")
    capital_tools = {
        "get_capital_flow",
        "get_top_list",
        "get_margin_detail",
        "get_chip_distribution",
        "tushare_query",
    }
    capital_caveats = ("未取得资金", "资金数据缺失", "无法判断主力", "尚未核验资金")
    if (
        any(term in text for term in capital_terms)
        and not verified_tools.intersection(capital_tools)
        and not any(term in text for term in capital_caveats)
    ):
        issues.append(
            {
                "code": "decision.unsupported_capital_claim",
                "description": "回答包含资金或机构结论，但没有经过验证的资金类工具证据。",
                "evidence_count": 0,
                "min_evidence": 1,
            }
        )
    return issues


def _company_answer_issues(
    answer: str,
    *,
    selected_skills: set[str],
) -> list[dict[str, Any]]:
    if "company-deep-research" not in selected_skills:
        return []
    text = answer.strip()
    requirements = (
        ("company.answer_summary", "报告必须包含执行摘要或核心结论。", ("执行摘要", "核心结论")),
        (
            "company.answer_as_of",
            "报告必须明确数据截止日期和行情交易日。",
            ("数据截止", "数据日期", "交易日", "截至"),
        ),
        ("company.answer_profile", "报告必须覆盖公司画像和主营业务。", ("公司画像", "主营业务")),
        (
            "company.answer_timeline",
            "报告必须提供行情与事件时间线。",
            ("事件时间线", "行情与事件", "时间线"),
        ),
        (
            "company.answer_financial_valuation",
            "报告必须同时覆盖财务质量和估值。",
            ("财务质量", "财务与估值", "估值"),
        ),
        (
            "company.answer_technical_capital",
            "报告必须覆盖技术量价和资金证据。",
            ("技术量价", "量价", "资金", "两融", "筹码"),
        ),
        (
            "company.answer_matrix",
            "报告必须给出多维决策矩阵或评分表。",
            ("多维决策矩阵", "决策矩阵", "评分表"),
        ),
        (
            "company.answer_counter",
            "报告必须给出最强反方论证。",
            ("最强反方", "反方论证", "反证"),
        ),
        (
            "company.answer_scenarios",
            "报告必须给出至少两种条件化方案。",
            ("方案A", "方案 A", "情景A", "情景 A"),
        ),
        (
            "company.answer_limits",
            "报告必须写明风险、失效条件和数据缺口。",
            ("失效条件", "数据缺口", "风险与局限"),
        ),
        (
            "company.answer_sources",
            "报告必须列出来源清单。",
            ("来源清单", "证据来源", "数据来源"),
        ),
    )
    issues = [
        {
            "code": code,
            "description": description,
            "evidence_count": 0,
            "min_evidence": 1,
        }
        for code, description, terms in requirements
        if not any(term in text for term in terms)
    ]
    scenario_markers = sum(
        marker in text
        for marker in ("方案A", "方案 A", "情景A", "情景 A", "方案B", "方案 B", "情景B", "情景 B")
    )
    if scenario_markers < 2 and not any(
        item["code"] == "company.answer_scenarios" for item in issues
    ):
        issues.append(
            {
                "code": "company.answer_scenarios",
                "description": "报告必须给出至少两种条件化方案。",
                "evidence_count": 0,
                "min_evidence": 1,
            }
        )
    return issues


class ResearchEvidenceService:
    def __init__(
        self,
        session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal,
    ) -> None:
        self.session_factory = session_factory

    def ensure_goal(
        self,
        *,
        run_id: str,
        user_id: int,
        objective: str,
        selected: list[SkillManifest],
    ) -> dict[str, Any] | None:
        if not selected:
            return None
        with self.session_factory() as db:
            goal = db.scalar(select(ResearchGoal).where(ResearchGoal.run_id == run_id).limit(1))
            if goal is None:
                goal = ResearchGoal(
                    goal_id=uuid.uuid4().hex,
                    run_id=run_id,
                    user_id=user_id,
                    objective=(objective or "完成研究任务").strip(),
                    selected_skills_json=json.dumps(
                        [item.name for item in selected], ensure_ascii=False
                    ),
                )
                db.add(goal)
                db.flush()
                for manifest in selected:
                    for criterion in manifest.criteria:
                        db.add(
                            ResearchGoalCriterion(
                                criterion_id=uuid.uuid4().hex,
                                goal_id=goal.goal_id,
                                criterion_code=criterion.code,
                                description=criterion.description,
                                required=criterion.required,
                                min_evidence=max(1, criterion.min_evidence),
                            )
                        )
                db.commit()
            return {
                "goal_id": goal.goal_id,
                "objective": goal.objective,
                "selected_skills": json.loads(goal.selected_skills_json or "[]"),
                "plan": _build_research_plan(goal.objective, selected),
            }

    def record_tool_result(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        result: dict[str, Any],
        execution_id: str | None,
        criterion_codes: list[str],
        success: bool,
    ) -> list[str]:
        with self.session_factory() as db:
            goal = db.scalar(select(ResearchGoal).where(ResearchGoal.run_id == run_id).limit(1))
            if goal is None:
                return []
            criteria = list(
                db.scalars(
                    select(ResearchGoalCriterion).where(
                        ResearchGoalCriterion.goal_id == goal.goal_id,
                        ResearchGoalCriterion.criterion_code.in_(criterion_codes or [""]),
                    )
                ).all()
            )
            targets: list[ResearchGoalCriterion | None] = criteria or [None]
            canonical = _canonical_json(result)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            evidence_ids: list[str] = []
            is_valid = bool(success and "error" not in result)
            for criterion in targets:
                definition = (
                    _criterion_definition(criterion.criterion_code)
                    if criterion is not None
                    else None
                )
                assessment = _assess_result(definition, tool_name, result, success)
                existing = db.scalar(
                    select(ResearchEvidence).where(
                        ResearchEvidence.goal_id == goal.goal_id,
                        ResearchEvidence.tool_call_id == tool_call_id,
                        ResearchEvidence.criterion_id
                        == (criterion.criterion_id if criterion is not None else None),
                    ).limit(1)
                )
                if existing is not None:
                    evidence_ids.append(existing.evidence_id)
                    continue
                evidence = ResearchEvidence(
                    evidence_id=uuid.uuid4().hex,
                    goal_id=goal.goal_id,
                    criterion_id=criterion.criterion_id if criterion else None,
                    run_id=run_id,
                    tool_execution_id=execution_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    source_provider=_provider_for(tool_name, result),
                    source_ref=_source_ref(result),
                    source_type=str(result.get("source_type") or "tool_result")[:80],
                    source_uri=_source_uri(result),
                    symbol_universe_json=json.dumps(
                        _symbol_universe(result), ensure_ascii=False
                    ),
                    timeframe=str(
                        result.get("timeframe")
                        or result.get("freq")
                        or result.get("period")
                        or ""
                    )[:120]
                    or None,
                    method=str(result.get("method") or tool_name)[:240],
                    assumptions_json=json.dumps(
                        result.get("assumptions") or {}, ensure_ascii=False, default=str
                    ),
                    data_as_of=assessment["data_as_of"],
                    freshness_status=assessment["freshness_status"],
                    verification_status=assessment["verification_status"],
                    confidence=assessment["confidence"],
                    caveat="; ".join(assessment["reasons"])[:4000] or None,
                    validation_json=json.dumps(assessment, ensure_ascii=False),
                    observed_at=datetime.utcnow(),
                    content_sha256=digest,
                    payload_json=canonical,
                    valid=bool(is_valid and assessment["verified"]),
                )
                db.add(evidence)
                evidence_ids.append(evidence.evidence_id)
            db.flush()
            for criterion in criteria:
                count = db.scalar(
                    select(func.count())
                    .select_from(ResearchEvidence)
                    .where(
                        ResearchEvidence.criterion_id == criterion.criterion_id,
                        ResearchEvidence.verification_status == "verified",
                    )
                )
                criterion.evidence_count = int(count or 0)
                criterion.status = (
                    "satisfied"
                    if criterion.evidence_count >= criterion.min_evidence
                    else "pending"
                )
            db.commit()
            return evidence_ids

    def audit(self, run_id: str, final_answer: str) -> dict[str, Any]:
        with self.session_factory() as db:
            goal = db.scalar(select(ResearchGoal).where(ResearchGoal.run_id == run_id).limit(1))
            if goal is None:
                return {"state": "not_applicable", "missing": [], "criteria": []}
            criteria = list(
                db.scalars(
                    select(ResearchGoalCriterion)
                    .where(ResearchGoalCriterion.goal_id == goal.goal_id)
                    .order_by(ResearchGoalCriterion.created_at, ResearchGoalCriterion.criterion_code)
                ).all()
            )
            required = [item for item in criteria if item.required]
            evidence_missing = [
                {
                    "code": item.criterion_code,
                    "description": item.description,
                    "evidence_count": item.evidence_count,
                    "min_evidence": item.min_evidence,
                }
                for item in required
                if item.evidence_count < item.min_evidence
            ]
            selected_skills = set(json.loads(goal.selected_skills_json or "[]"))
            verified_tools = set(
                db.scalars(
                    select(ResearchEvidence.tool_name).where(
                        ResearchEvidence.goal_id == goal.goal_id,
                        ResearchEvidence.verification_status == "verified",
                    )
                ).all()
            )
            quality_issues = _decision_answer_issues(
                final_answer,
                selected_skills=selected_skills,
                verified_tools=verified_tools,
            )
            quality_issues.extend(
                _company_answer_issues(
                    final_answer,
                    selected_skills=selected_skills,
                )
            )
            missing = [*evidence_missing, *quality_issues]
            state = "complete" if not missing else "incomplete"
            if final_answer.strip():
                db.add(
                    ResearchClaim(
                        claim_id=uuid.uuid4().hex,
                        goal_id=goal.goal_id,
                        run_id=run_id,
                        statement=final_answer.strip()[:16000],
                        claim_type="final_answer",
                        status="supported" if state == "complete" else "provisional",
                    )
                )
            summary = {
                "state": state,
                "goal_id": goal.goal_id,
                "required_count": len(required) + len(quality_issues),
                "satisfied_count": len(required) - len(evidence_missing),
                "missing": missing,
                "quality_issues": quality_issues,
                "criteria": [
                    {
                        "code": item.criterion_code,
                        "description": item.description,
                        "required": item.required,
                        "status": item.status,
                        "evidence_count": item.evidence_count,
                        "min_evidence": item.min_evidence,
                    }
                    for item in criteria
                ],
            }
            next_sequence = int(
                db.scalar(
                    select(func.max(ResearchCompletionAudit.sequence)).where(
                        ResearchCompletionAudit.goal_id == goal.goal_id
                    )
                )
                or 0
            ) + 1
            db.add(
                ResearchCompletionAudit(
                    audit_id=uuid.uuid4().hex,
                    goal_id=goal.goal_id,
                    run_id=run_id,
                    sequence=next_sequence,
                    completion_state=state,
                    required_count=summary["required_count"],
                    satisfied_count=summary["satisfied_count"],
                    missing_json=json.dumps(missing, ensure_ascii=False),
                    summary_json=json.dumps(summary, ensure_ascii=False),
                )
            )
            goal.status = state
            db.commit()
            return summary

    def audit_bundle(self, run_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            goal = db.scalar(select(ResearchGoal).where(ResearchGoal.run_id == run_id).limit(1))
            if goal is None:
                return None
            criteria = list(
                db.scalars(
                    select(ResearchGoalCriterion)
                    .where(ResearchGoalCriterion.goal_id == goal.goal_id)
                    .order_by(ResearchGoalCriterion.created_at, ResearchGoalCriterion.criterion_code)
                ).all()
            )
            evidence = list(
                db.scalars(
                    select(ResearchEvidence)
                    .where(ResearchEvidence.goal_id == goal.goal_id)
                    .order_by(ResearchEvidence.created_at, ResearchEvidence.evidence_id)
                ).all()
            )
            claims = list(
                db.scalars(
                    select(ResearchClaim)
                    .where(ResearchClaim.goal_id == goal.goal_id)
                    .order_by(ResearchClaim.created_at, ResearchClaim.claim_id)
                ).all()
            )
            latest_audit = db.scalar(
                select(ResearchCompletionAudit)
                .where(ResearchCompletionAudit.goal_id == goal.goal_id)
                .order_by(
                    ResearchCompletionAudit.sequence.desc(),
                )
                .limit(1)
            )
            return {
                "goal": {
                    "goal_id": goal.goal_id,
                    "objective": goal.objective,
                    "status": goal.status,
                    "selected_skills": json.loads(goal.selected_skills_json or "[]"),
                },
                "criteria": [
                    {
                        "criterion_id": item.criterion_id,
                        "code": item.criterion_code,
                        "description": item.description,
                        "required": item.required,
                        "min_evidence": item.min_evidence,
                        "evidence_count": item.evidence_count,
                        "status": item.status,
                    }
                    for item in criteria
                ],
                "evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "criterion_id": item.criterion_id,
                        "tool_call_id": item.tool_call_id,
                        "tool_name": item.tool_name,
                        "source_provider": item.source_provider,
                        "source_ref": item.source_ref,
                        "source_type": item.source_type,
                        "source_uri": item.source_uri,
                        "symbol_universe": json.loads(item.symbol_universe_json or "[]"),
                        "timeframe": item.timeframe,
                        "method": item.method,
                        "data_as_of": item.data_as_of,
                        "freshness_status": item.freshness_status,
                        "verification_status": item.verification_status,
                        "confidence": item.confidence,
                        "caveat": item.caveat,
                        "validation": json.loads(item.validation_json or "{}"),
                        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                        "content_sha256": item.content_sha256,
                        "valid": item.valid,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                    for item in evidence
                ],
                "claims": [
                    {
                        "claim_id": item.claim_id,
                        "statement": item.statement,
                        "claim_type": item.claim_type,
                        "confidence": item.confidence,
                        "status": item.status,
                    }
                    for item in claims
                ],
                "latest_audit": (
                    json.loads(latest_audit.summary_json) if latest_audit else None
                ),
            }


research_evidence_service = ResearchEvidenceService()
