"""统一日志中枢：分门别类的 Python logging + 重要业务事件双写 system_logs 表。

两条日志线，各司其职：
- **进程日志**（文件 + 控制台）：所有 get_logger(category) / log_event 都会输出到
  logs/app.log（全量、轮转）与 logs/error.log（WARNING+）。用于运维排查，含请求、
  LLM 细节等高频信息。logger 名统一为 hermes.<category>，可按类别 grep / 调级别。
- **业务日志**（system_logs 表）：log_event(..., to_db=True) 额外写一行 SystemLog
  （module=category），供前端「系统状态」可视化与历史查询。高频信息用 to_db=False
  只落文件，避免撑爆数据库。

日志分类（category / module）：
  system     启动、生命周期、组件启停
  auth       登录、登出、鉴权失败
  api        HTTP 请求（中间件）
  agent      ReAct / LLM 对话
  agent_sql  run_sql 只读沙箱审计
  scheduler  定时调度（每日节律）
  feishu     飞书 webhook 推送
  feishu_bot 飞书机器人长连接 / 对话
  position   持仓录入（自然语言 / 截图）
  db         数据库迁移 / 结构
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from pathlib import Path

from .settings import settings

_LOGGER_PREFIX = "hermes"
_configured = False
_config_lock = threading.Lock()

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

KNOWN_CATEGORIES = (
    "system", "auth", "api", "agent", "agent_sql",
    "scheduler", "feishu", "feishu_bot", "position", "db",
)


def _log_dir() -> Path:
    base = getattr(settings, "log_dir", "") or "logs"
    path = Path(base)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / base
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging() -> None:
    """幂等配置根 logger：轮转文件 + 控制台。多次调用只生效一次。"""
    global _configured
    with _config_lock:
        if _configured:
            return
        level = _LEVELS.get(getattr(settings, "log_level", "INFO").upper(), logging.INFO)
        root = logging.getLogger(_LOGGER_PREFIX)
        root.setLevel(level)
        root.propagate = False
        fmt = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        try:
            log_dir = _log_dir()
            app_handler = logging.handlers.RotatingFileHandler(
                log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            app_handler.setLevel(level)
            app_handler.setFormatter(fmt)
            root.addHandler(app_handler)

            err_handler = logging.handlers.RotatingFileHandler(
                log_dir / "error.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            err_handler.setLevel(logging.WARNING)
            err_handler.setFormatter(fmt)
            root.addHandler(err_handler)
        except OSError:
            pass  # 文件不可写时至少保留控制台

        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(fmt)
        root.addHandler(console)

        _configured = True


def get_logger(category: str) -> logging.Logger:
    """按类别取 logger（名为 hermes.<category>）。"""
    if not _configured:
        configure_logging()
    return logging.getLogger(f"{_LOGGER_PREFIX}.{category}")


def _level_no(level: str) -> int:
    return _LEVELS.get((level or "INFO").upper(), logging.INFO)


def _write_db(level: str, category: str, message: str, context: dict | None) -> None:
    # 延迟导入，避免 applog ← models ← database 的循环
    from .database import SessionLocal
    from .models import SystemLog

    try:
        with SessionLocal() as session:
            session.add(
                SystemLog(
                    level=(level or "INFO").upper(),
                    module=category,
                    message=message,
                    context_json=json.dumps(context, ensure_ascii=False) if context else None,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 —— 日志失败绝不能影响主流程
        get_logger("system").warning("写入 system_logs 失败（已忽略）", exc_info=True)


def prune_system_logs(max_age_days: int | None = None) -> int:
    """删除超过保留期的 system_logs 行，返回删除条数。

    system_logs 是只增表，长跑进程会无限膨胀；按 settings.log_retention_days
    定期清理（启动时 + 每日健康检查各调一次）。失败只告警、不抛出。
    max_age_days<=0 视为永久保留，直接跳过。
    """
    from datetime import datetime, timedelta

    from .database import SessionLocal
    from .models import SystemLog

    days = max_age_days if max_age_days is not None else getattr(settings, "log_retention_days", 90)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 90
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    try:
        with SessionLocal() as session:
            deleted = (
                session.query(SystemLog)
                .filter(SystemLog.created_at < cutoff)
                .delete(synchronize_session=False)
            )
            session.commit()
        deleted = int(deleted or 0)
        if deleted:
            get_logger("system").info(f"清理 system_logs：删除 {deleted} 条超过 {days} 天的日志")
        return deleted
    except Exception:  # noqa: BLE001 —— 清理失败绝不能影响主流程
        get_logger("system").warning("清理 system_logs 失败（已忽略）", exc_info=True)
        return 0


def log_event(
    category: str,
    level: str,
    message: str,
    *,
    to_db: bool = True,
    context: dict | None = None,
    **extra,
) -> None:
    """记录一条业务事件：进程日志（永远）+ system_logs 表（to_db=True 时）。

    context / **extra 合并为结构化上下文，进程日志里以 JSON 尾随，DB 里存 context_json。
    """
    ctx = dict(context or {})
    ctx.update(extra)
    logger = get_logger(category)
    suffix = f"  {json.dumps(ctx, ensure_ascii=False)}" if ctx else ""
    logger.log(_level_no(level), f"{message}{suffix}")
    if to_db:
        _write_db(level, category, message, ctx or None)
