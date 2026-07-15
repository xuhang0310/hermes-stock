"""修复生产环境 DB 中损坏的 instrument / snapshot 名称。只读扫描 + 修补。"""
from backend.app.core.database import SessionLocal
from backend.app.core.models import Instrument, PositionHoldingSnapshot
from backend.app.market.stock_tools import get_stock_info
from sqlalchemy import select

def _has_chinese(s: str) -> bool:
    return any('一' <= c <= '鿿' for c in (s or ''))

with SessionLocal() as session:
    # 1) 修复 instruments
    rows = list(session.execute(select(Instrument)).scalars().all())
    bad = [r for r in rows if not _has_chinese(r.name or '')]
    fixed = 0
    for r in bad:
        new_name = None
        try:
            info = get_stock_info(r.symbol)
            if info and isinstance(info, dict) and 'error' not in info:
                new_name = info.get('name', '') or None
        except Exception:
            pass
        if new_name:
            r.name = new_name
            fixed += 1
            print(f"  fixed instrument {r.symbol} -> {new_name}")
        else:
            print(f"  skip {r.symbol} (no TuShare name)")
    if fixed:
        session.commit()
        print(f"fixed {fixed} instruments")

    # 2) 修复 snapshots
    instruments = {r.symbol: r.name for r in rows}
    snaps = list(session.execute(select(PositionHoldingSnapshot)).scalars().all())
    sfixed = 0
    for s in snaps:
        expected = instruments.get(s.symbol, s.symbol)
        if _has_chinese(s.symbol_name or ''):
            continue
        if s.symbol_name != expected:
            s.symbol_name = expected
            sfixed += 1
    if sfixed:
        session.commit()
        print(f"fixed {sfixed} snapshots")

    print("Done")
