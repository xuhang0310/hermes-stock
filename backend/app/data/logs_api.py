from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository


router = APIRouter()


@router.get("/api/logs")
def logs(
    repository: StockRepository = Depends(get_repo),
    limit: int = Query(default=100, ge=1, le=500),
    module: str | None = Query(default=None),
    level: str | None = Query(default=None),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[dict]:
    return repository.system_logs(limit, module=module, level=level)


@router.get("/api/logs/modules")
def log_modules(
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[str]:
    return repository.system_log_modules()