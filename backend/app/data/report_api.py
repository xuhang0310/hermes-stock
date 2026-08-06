"""研究报告详情与导出接口。

单一职责：只负责"报告读全量 + 导出成文件"这一表现层；数据查询走
``StockRepository.report_detail`` / ``reports``，本模块不写 ORM 查询。

端点：
- GET /api/reports/export.csv          报告列表 CSV（归档用，BOM 兼容 Excel）
- GET /api/reports/{report_id}         单篇全量结构化字段（ok 信封）
- GET /api/reports/{report_id}/export  单篇导出，format=md|json（默认 md）

导出为文件下载，直接返回 Response（与 backtest trades.csv/metrics.json 一致），
不套 ok/fail 信封；详情查询是新增 JSON 端点，统一用信封。
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository

router = APIRouter(tags=["reports"])

_CSV_LIMIT = 500
_EXPORT_COLUMNS = [
    "report_id",
    "agent_name",
    "display_name",
    "symbol",
    "symbol_name",
    "title",
    "action_suggestion",
    "conclusion",
    "risk_points",
    "model_name",
    "created_at",
]

# 结构化段落：字段名 -> Markdown 小节标题（按报告叙事顺序）
_MD_SECTIONS = (
    ("conclusion", "结论"),
    ("reasoning", "论证过程"),
    ("counter_arguments", "反方观点"),
    ("risk_points", "风险点"),
    ("position_limit", "仓位纪律"),
    ("stop_condition", "退出条件"),
)


def _render_markdown(detail: dict) -> str:
    """把结构化报告渲染成 Markdown 全文（空段落自动省略）。"""
    lines = [f"# {detail.get('title') or '研究报告'}", ""]
    meta = []
    if detail.get("display_name") or detail.get("agent_name"):
        meta.append(f"智能体：{detail.get('display_name') or detail.get('agent_name')}")
    if detail.get("symbol"):
        symbol_label = detail["symbol"]
        if detail.get("symbol_name"):
            symbol_label = f"{detail['symbol_name']}（{detail['symbol']}）"
        meta.append(f"标的：{symbol_label}")
    if detail.get("action_suggestion"):
        meta.append(f"操作建议：{detail['action_suggestion']}")
    if detail.get("model_name"):
        meta.append(f"模型：{detail['model_name']}")
    if detail.get("created_at"):
        meta.append(f"生成时间：{detail['created_at']}")
    if meta:
        lines.extend(f"- {item}" for item in meta)
        lines.append("")
    for field, heading in _MD_SECTIONS:
        value = (detail.get(field) or "").strip()
        if value:
            lines.extend([f"## {heading}", "", value, ""])
    content = (detail.get("content") or "").strip()
    if content:
        lines.extend(["---", "", content, ""])
    return "\n".join(lines)


@router.get("/api/reports/export.csv")
def reports_export_csv(
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> Response:
    """导出最近的报告列表为 CSV（摘要字段，不含全文）。"""
    rows = repository.reports(limit=_CSV_LIMIT)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="agent-reports.csv"'},
    )


@router.get("/api/reports/{report_id}")
def report_detail(
    report_id: str,
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    detail = repository.report_detail(report_id)
    if detail is None:
        return JSONResponse(status_code=404, content=fail("报告不存在", code="not_found"))
    return ok(detail)


@router.get("/api/reports/{report_id}/export")
def report_export(
    report_id: str,
    format: str = Query(default="md", pattern="^(md|json)$"),
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> Response:
    """单篇报告导出：md=Markdown 全文，json=结构化字段。"""
    detail = repository.report_detail(report_id)
    if detail is None:
        return JSONResponse(status_code=404, content=fail("报告不存在", code="not_found"))
    if format == "json":
        return Response(
            content=json.dumps(detail, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report_id}.json"'},
        )
    return Response(
        content=_render_markdown(detail),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report_id}.md"'},
    )
