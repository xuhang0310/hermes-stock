"""启动校验器：进程启动时把"配错就一定会出事"的依赖提前暴露。

单一职责：只做启动前的配置/依赖校验并给出结构化报告，不做修复、不做业务。

两级：
- REQUIRED（必需）：DB 可达。失败默认 fail-fast（抛异常阻止启动）。
- RECOMMENDED（建议）：auth_secret 未配置（应用会以随机密钥降级运行，重启后
  会话失效，但不阻断启动）、LLM/Tushare/Tavily 等 key 的占位符检测。失败只告警。

占位符检测针对常见的"还没填"取值（``your_*``、``changeme``、``<...>``、纯 ``x`` 串等）。
"""
from __future__ import annotations

from backend.app.core.applog import get_logger
from backend.app.core.settings import settings

_PLACEHOLDER_MARKERS = ("your_", "your-", "changeme", "change_me", "placeholder", "todo", "<", ">")


def _is_missing(value: str | None) -> bool:
    return not (value or "").strip()


def _is_placeholder(value: str | None) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return True
    if any(marker in text for marker in _PLACEHOLDER_MARKERS):
        return True
    # 形如 "xxxx" / "sk-xxx" 的掩码占位
    stripped = text.replace("-", "").replace("_", "")
    if stripped and len(set(stripped)) == 1:
        return True
    return False


def _check_database() -> tuple[bool, str]:
    from sqlalchemy import text

    from backend.app.core.database import SessionLocal

    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return True, "MySQL 连接正常"
    except Exception as exc:  # noqa: BLE001
        return False, f"MySQL 连接失败：{exc}"


def _check_auth_secret() -> tuple[bool, str]:
    if _is_missing(settings.auth_secret):
        return False, "STOCK_AUTH_SECRET 未设置（将用随机密钥，重启后所有会话失效）"
    if len(settings.auth_secret) < 16:
        return False, "STOCK_AUTH_SECRET 过短（建议至少 32 字符）"
    return True, "auth_secret 已配置"


def validate_startup(*, fail_fast: bool = True) -> dict:
    """执行全部启动校验，返回结构化报告；REQUIRED 失败且 fail_fast 时抛 RuntimeError。"""
    log = get_logger("system")

    required: list[dict] = []
    db_ok, db_detail = _check_database()
    required.append({"name": "database", "ok": db_ok, "detail": db_detail})

    recommended: list[dict] = []
    secret_ok, secret_detail = _check_auth_secret()
    recommended.append({"name": "STOCK_AUTH_SECRET", "ok": secret_ok, "detail": secret_detail})
    for name, value in (
        ("STOCK_LLM_API_KEY", settings.llm_api_key),
        ("STOCK_TUSHARE_TOKEN", settings.tushare_token),
        ("STOCK_TAVILY_API_KEY", settings.tavily_api_key),
    ):
        usable = not _is_placeholder(value)
        recommended.append(
            {
                "name": name,
                "ok": usable,
                "detail": "已配置" if usable else "未配置或疑似占位符（相关功能将降级）",
            }
        )

    report = {"required": required, "recommended": recommended}

    for item in recommended:
        if not item["ok"]:
            log.warning("启动校验[建议] %s：%s", item["name"], item["detail"])

    failed = [item for item in required if not item["ok"]]
    for item in failed:
        log.error("启动校验[必需] %s：%s", item["name"], item["detail"])

    if failed:
        message = "；".join(f"{item['name']}: {item['detail']}" for item in failed)
        if fail_fast:
            raise RuntimeError(f"启动校验失败：{message}")
        log.error("启动校验失败（fail_fast=False，继续启动）：%s", message)
    else:
        log.info("启动校验通过：必需项全部就绪")

    return report
