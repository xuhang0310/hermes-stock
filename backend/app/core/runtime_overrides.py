"""运行时配置覆盖：少数白名单参数用 DB key/value 覆盖环境变量默认值，免重启生效。

单一职责：只负责白名单覆盖值的存取与解析——不修改 settings 对象本身，
不做业务逻辑；消费方通过 ``resolve_*`` 拿"覆盖值或默认值"。

安全边界：
- 仅 ``SUPPORTED_OVERRIDES`` 白名单内的参数可覆盖（杜绝任意配置注入）。
- 写入时做类型与范围校验；读取/解析失败一律降级默认值并告警，不抛异常。
"""
from __future__ import annotations

from typing import Any

from backend.app.core.applog import get_logger
from backend.app.core.database import SessionLocal

_log = get_logger("system")

# 白名单：key -> 类型/范围/说明。新增可覆盖参数必须先在这里登记。
SUPPORTED_OVERRIDES: dict[str, dict[str, Any]] = {
    "agent_max_steps": {
        "type": "int",
        "min": 1,
        "max": 50,
        "description": "智能体 ReAct 循环最大步数（覆盖 STOCK_AGENT_MAX_STEPS）",
    },
}


def _coerce(key: str, raw: str) -> Any:
    """按白名单类型解析原始值；非法抛 ValueError。"""
    spec = SUPPORTED_OVERRIDES[key]
    if spec["type"] == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要整数") from exc
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"{key} 取值须在 {spec['min']}~{spec['max']} 之间")
        return value
    raise ValueError(f"不支持的参数类型：{spec['type']}")


def get_override(key: str) -> Any | None:
    """读取覆盖值（已按类型解析）；未设置或读取失败返回 None。"""
    from backend.app.core.models import RuntimeConfigOverride

    try:
        with SessionLocal() as session:
            row = session.get(RuntimeConfigOverride, key)
            if row is None:
                return None
            return _coerce(key, row.value)
    except ValueError:
        _log.warning("运行时覆盖值非法（已忽略）：key=%s", key)
        return None
    except Exception:  # noqa: BLE001 —— 配置读取失败绝不能阻断主流程
        _log.warning("读取运行时覆盖失败（已降级默认值）：key=%s", key, exc_info=True)
        return None


def resolve_int(key: str, default: int) -> int:
    """取 int 覆盖值，未设置则用默认值。"""
    value = get_override(key)
    return value if isinstance(value, int) else default


def agent_max_steps(default: int) -> int:
    """agent_max_steps 的便捷解析（消费方语义自文档化）。"""
    return resolve_int("agent_max_steps", default)


def set_override(key: str, value: str) -> dict:
    """写入覆盖值（幂等 upsert）；key 不在白名单或值非法抛 ValueError。"""
    from backend.app.core.models import RuntimeConfigOverride

    if key not in SUPPORTED_OVERRIDES:
        raise ValueError(f"不支持的配置项：{key}")
    coerced = _coerce(key, value)  # 先校验再落库
    with SessionLocal() as session:
        row = session.get(RuntimeConfigOverride, key)
        if row is None:
            row = RuntimeConfigOverride(key=key, value=str(coerced))
            session.add(row)
        else:
            row.value = str(coerced)
        session.commit()
    return {"key": key, "value": coerced}


def delete_override(key: str) -> bool:
    """删除覆盖值（恢复环境变量默认）；不存在返回 False。"""
    from backend.app.core.models import RuntimeConfigOverride

    with SessionLocal() as session:
        deleted = (
            session.query(RuntimeConfigOverride)
            .filter(RuntimeConfigOverride.key == key)
            .delete(synchronize_session=False)
        )
        session.commit()
        return bool(deleted)


def list_overrides() -> list[dict]:
    """列出所有白名单参数的当前状态（覆盖值 + 说明），供管理页展示。"""
    from backend.app.core.models import RuntimeConfigOverride

    stored: dict[str, str] = {}
    try:
        with SessionLocal() as session:
            for row in session.query(RuntimeConfigOverride).all():
                stored[row.key] = row.value
    except Exception:  # noqa: BLE001
        _log.warning("列出运行时覆盖失败（返回空覆盖）", exc_info=True)
    items = []
    for key, spec in SUPPORTED_OVERRIDES.items():
        raw = stored.get(key)
        try:
            resolved = None if raw is None else _coerce(key, raw)
        except ValueError:  # 库里存了非法值（如手改 DB）：展示原始值、解析值置空
            resolved = None
        items.append(
            {
                "key": key,
                "description": spec["description"],
                "override": raw,
                "resolved": resolved,
            }
        )
    return items
