from __future__ import annotations


def target_roles_allow(raw: str | None, role: str | None) -> bool:
    if not raw or not role:
        return False
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in allowed or role in allowed
