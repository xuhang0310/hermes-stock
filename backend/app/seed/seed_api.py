from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import get_db
from backend.app.seed.seed import seed_demo


router = APIRouter()


@router.post("/api/dev/seed-demo")
def api_seed_demo(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    seed_demo(db)
    return {"ok": True, "message": "demo data inserted"}