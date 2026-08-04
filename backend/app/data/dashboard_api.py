from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository


router = APIRouter()


@router.get("/api/dashboard")
def dashboard(
    response: Response,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return repository.for_user(context.user.id).dashboard(user_id=context.user.id)


@router.get("/api/events")
def events(
    repository: StockRepository = Depends(get_repo),
    limit: int = Query(default=50, ge=1, le=200),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.events(limit)


@router.get("/api/events/{event_id}")
def event_detail(
    event_id: str,
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    event = repository.event_detail(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@router.get("/api/agents")
def agents(
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("agent:use")),
) -> list[dict]:
    return repository.agents()


@router.get("/api/reports")
def reports(
    repository: StockRepository = Depends(get_repo),
    limit: int = Query(default=50, ge=1, le=200),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.reports(limit)


@router.get("/api/risk-checks")
def risk_checks(
    repository: StockRepository = Depends(get_repo),
    limit: int = Query(default=50, ge=1, le=200),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.risk_checks(limit)