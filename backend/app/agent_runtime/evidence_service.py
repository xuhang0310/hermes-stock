from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AgentRun,
    ResearchClaim,
    ResearchCompletionAudit,
    ResearchEvidence,
    ResearchGoal,
    ResearchGoalCriterion,
)
from backend.app.skills.manifests import SkillManifest
from backend.app.skills.manifests import CORE_SKILL_MANIFESTS, SkillCriterion


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

    as_of = _data_as_of(result)
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
            missing = [
                {
                    "code": item.criterion_code,
                    "description": item.description,
                    "evidence_count": item.evidence_count,
                    "min_evidence": item.min_evidence,
                }
                for item in required
                if item.evidence_count < item.min_evidence
            ]
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
                "required_count": len(required),
                "satisfied_count": len(required) - len(missing),
                "missing": missing,
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
                    required_count=len(required),
                    satisfied_count=len(required) - len(missing),
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
