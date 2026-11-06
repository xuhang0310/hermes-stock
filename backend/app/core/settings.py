from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

from backend.app.core.api_keys import merge_api_keys


BASE_DIR = Path(__file__).resolve().parents[2]  # core/ -> app/ -> backend/（backend/.env 所在）
# 仅在开发者本机可用作备用值源（存在才加载，不存在就跳过，不阻塞其他机器）
_RABBIT_ENV_RAW = os.getenv("STOCK_RABBIT_ENV_PATH", "").strip()
_RABBIT_ENV = Path(_RABBIT_ENV_RAW).expanduser() if _RABBIT_ENV_RAW else Path("__missing_rabbit_env__")
if not _RABBIT_ENV_RAW or not _RABBIT_ENV.is_file():
    _RABBIT_ENV = Path(os.path.expanduser("~/Documents/hermes-rabbit/backend/.env"))
if _RABBIT_ENV.is_file():
    load_dotenv(_RABBIT_ENV, override=False)
load_dotenv(BASE_DIR / ".env", override=True)


@dataclass(frozen=True)
class Settings:
    db_host: str = os.getenv("STOCK_DB_HOST", os.getenv("RABBIT_DB_HOST", "127.0.0.1"))
    db_port: int = int(os.getenv("STOCK_DB_PORT", os.getenv("RABBIT_DB_PORT", "3306")))
    db_user: str = os.getenv("STOCK_DB_USER", os.getenv("RABBIT_DB_USER", "root"))
    db_password: str = os.getenv("STOCK_DB_PASSWORD", os.getenv("RABBIT_DB_PASSWORD", ""))
    db_name: str = os.getenv("STOCK_DB_NAME", "hermes-stock")
    db_echo: bool = os.getenv("STOCK_DB_ECHO", "false").lower() == "true"
    auth_secret: str = os.getenv("STOCK_AUTH_SECRET", "")
    auth_token_ttl_hours: int = int(os.getenv("STOCK_AUTH_TOKEN_TTL_HOURS", "24"))
    auth_remember_token_ttl_days: int = int(os.getenv("STOCK_AUTH_REMEMBER_TOKEN_TTL_DAYS", "30"))
    auth_login_max_attempts: int = int(os.getenv("STOCK_AUTH_LOGIN_MAX_ATTEMPTS", "8"))
    auth_login_window_seconds: int = int(os.getenv("STOCK_AUTH_LOGIN_WINDOW_SECONDS", "900"))
    webhook_enc_key: str = os.getenv("STOCK_WEBHOOK_ENC_KEY", "")
    llm_base_url: str = os.getenv("STOCK_LLM_BASE_URL", os.getenv("RABBIT_LLM_BASE_URL", ""))
    llm_api_key: str = os.getenv("STOCK_LLM_API_KEY", os.getenv("RABBIT_LLM_API_KEY", ""))
    llm_model: str = os.getenv("STOCK_LLM_MODEL", os.getenv("RABBIT_LLM_MODEL", "gpt-4.1-mini"))
    agent_max_steps: int = int(os.getenv("STOCK_AGENT_MAX_STEPS", os.getenv("RABBIT_AGENT_MAX_STEPS", "6")))
    tushare_token: str = os.getenv("STOCK_TUSHARE_TOKEN", os.getenv("TUSHARE_TOKEN", ""))
    # Keep the legacy single-key setting as the primary. The plural setting is
    # a comma/newline-separated fallback pool.
    tavily_api_keys: tuple[str, ...] = merge_api_keys(
        os.getenv("STOCK_TAVILY_API_KEY", os.getenv("TAVILY_API_KEY", "")),
        os.getenv("STOCK_TAVILY_API_KEYS", ""),
    )
    tavily_api_key: str = tavily_api_keys[0] if tavily_api_keys else ""
    brave_api_key: str = os.getenv("STOCK_BRAVE_API_KEY", os.getenv("BRAVE_API_KEY", ""))
    # 向量记忆嵌入服务（与聊天模型解耦；未配置则回退活跃 LLM 提供商的 /embeddings）
    embedding_base_url: str = os.getenv("STOCK_EMBEDDING_BASE_URL", "")
    embedding_api_key: str = os.getenv("STOCK_EMBEDDING_API_KEY", "")
    embedding_model: str = os.getenv("STOCK_EMBEDDING_MODEL", "")
    # 记忆检索重排模型（复用 embedding 的 base_url/api_key，走同一提供商的 /rerank）；空=关闭重排
    rerank_model: str = os.getenv("STOCK_RERANK_MODEL", "")
    # 向量库后端：chroma（嵌入式，默认，本地/Windows）| milvus（服务端）
    vector_backend: str = os.getenv("STOCK_VECTOR_BACKEND", "chroma")
    chroma_dir: str = os.getenv("STOCK_CHROMA_DIR", "")
    experience_chroma_dir: str = os.getenv("STOCK_EXP_CHROMA_DIR", "")
    milvus_uri: str = os.getenv("STOCK_MILVUS_URI", "http://127.0.0.1:19530")
    milvus_token: str = os.getenv("STOCK_MILVUS_TOKEN", "")
    # RAG 知识库（P1）：切块/检索参数；向量后端沿用 vector_backend/milvus_uri，collection 独立
    rag_enabled: bool = os.getenv("STOCK_RAG_ENABLED", "true").lower() == "true"
    rag_milvus_collection: str = os.getenv("STOCK_RAG_MILVUS_COLLECTION", "knowledge_chunks")
    experience_milvus_collection: str = os.getenv("STOCK_EXP_MILVUS_COLLECTION", "experience_entries")
    rag_top_k: int = int(os.getenv("STOCK_RAG_TOP_K", "6"))
    rag_chunk_size: int = int(os.getenv("STOCK_RAG_CHUNK_SIZE", "700"))
    rag_chunk_overlap: int = int(os.getenv("STOCK_RAG_OVERLAP", "80"))
    rag_min_score: float = float(os.getenv("STOCK_RAG_MIN_SCORE", "0.35"))  # P2 注入阈值，先建
    # 查询改写+多路扩展（RAG P2）：检索/联网搜索前用一次 LLM 把口语 query 改写并扩成多角度查询，
    # 提高召回。成本=每次搜索多一次 LLM 调用；关闭则降级为原单查询（行为与改造前一致）。
    query_rewrite_enabled: bool = os.getenv("STOCK_QUERY_REWRITE_ENABLED", "true").lower() == "true"
    # 说人话改稿（ai/plain_rewrite.py）：定时报告推送前多一次 LLM 调用，只改说法不改事实，
    # 过不了数字核对就沿用原文。关闭则直接推送数字人原文。
    plain_rewrite_enabled: bool = os.getenv("STOCK_PLAIN_REWRITE_ENABLED", "true").lower() == "true"
    # 飞书推送附带的手机版网页（data/share_api.py）：配了对外地址才加按钮，没配时推送和原来完全一样
    public_base_url: str = os.getenv("STOCK_PUBLIC_BASE_URL", "").strip().rstrip("/")
    share_ttl_days: int = int(os.getenv("STOCK_SHARE_TTL_DAYS", "30"))
    # 报告类推送有手机版链接时，卡片只放一句核心的话和按钮；false 恢复全文卡片
    feishu_brief_card: bool = os.getenv("STOCK_FEISHU_BRIEF_CARD", "true").lower() == "true"
    # 立案、停牌这类重大消息夜里（22:30 到 07:30）也推；false 就并入第二天早盘
    event_night_push: bool = os.getenv("STOCK_EVENT_NIGHT_PUSH", "true").lower() == "true"
    # 卖价和止损价由量化统计表算，数字人改不了（plans/quant_orders）；false 恢复由数字人定价
    quant_orders_enabled: bool = os.getenv("STOCK_QUANT_ORDERS_ENABLED", "true").lower() == "true"
    quant_sizes_enabled: bool = os.getenv("STOCK_QUANT_SIZES_ENABLED", "true").lower() == "true"
    # Agent 工作区（文件读写/查找/执行）：受控 scratch 目录，与项目代码/.env/DB 物理隔离
    workspace_enabled: bool = os.getenv("STOCK_WORKSPACE_ENABLED", "true").lower() == "true"
    # 执行能力独立开关，默认关：开启=给聊天用户放开"以应用用户身份执行 Python"，生产须在独立低权账户/沙箱下才开
    workspace_exec_enabled: bool = os.getenv("STOCK_WORKSPACE_EXEC_ENABLED", "false").lower() == "true"
    # 执行后端：生产固定用 docker；local 仅供受信任的开发环境显式启用。
    workspace_exec_backend: str = os.getenv("STOCK_WORKSPACE_EXEC_BACKEND", "docker")
    workspace_exec_socket: str = os.getenv(
        "STOCK_WORKSPACE_EXEC_SOCKET", "/run/hermes-stock-sandbox/sandbox.sock"
    )
    workspace_exec_image: str = os.getenv(
        "STOCK_WORKSPACE_EXEC_IMAGE", "hermes-stock-python-sandbox:latest"
    )
    workspace_exec_memory: str = os.getenv("STOCK_WORKSPACE_EXEC_MEMORY", "1g")
    workspace_exec_cpus: float = float(os.getenv("STOCK_WORKSPACE_EXEC_CPUS", "2"))
    workspace_exec_pids_limit: int = int(os.getenv("STOCK_WORKSPACE_EXEC_PIDS_LIMIT", "128"))
    workspace_exec_tmpfs_bytes: int = int(os.getenv("STOCK_WORKSPACE_EXEC_TMPFS_BYTES", str(256 * 1024 * 1024)))
    workspace_exec_file_bytes: int = int(os.getenv("STOCK_WORKSPACE_EXEC_FILE_BYTES", str(64 * 1024 * 1024)))
    workspace_exec_workspace_bytes: int = int(
        os.getenv("STOCK_WORKSPACE_EXEC_WORKSPACE_BYTES", str(256 * 1024 * 1024))
    )
    workspace_exec_workspace_files: int = int(os.getenv("STOCK_WORKSPACE_EXEC_WORKSPACE_FILES", "2000"))
    workspace_exec_max_concurrency: int = int(os.getenv("STOCK_WORKSPACE_EXEC_MAX_CONCURRENCY", "4"))
    # Durable Agent runtime. The legacy engine remains an explicit rollback
    # switch, while new installations use AgentScope directly.
    agent_runtime_mode: str = os.getenv("STOCK_AGENT_RUNTIME_MODE", "agentscope").strip().lower()
    agent_runtime_worker_enabled: bool = os.getenv(
        "STOCK_AGENT_RUNTIME_WORKER_ENABLED",
        os.getenv("STOCK_BACKGROUND_SERVICES_ENABLED", "true"),
    ).lower() == "true"
    agent_redis_url: str = os.getenv("STOCK_AGENT_REDIS_URL", "").strip()
    agent_run_timeout_seconds: int = int(os.getenv("STOCK_AGENT_RUN_TIMEOUT_SECONDS", "900"))
    # AgentScope ReAct loop budget. This is intentionally independent from
    # model-level max_steps, which remains available to the legacy runtime.
    agent_loop_max_iterations: int = int(os.getenv("STOCK_AGENT_LOOP_MAX_ITERATIONS", "50"))
    agent_loop_duplicate_tool_limit: int = int(
        os.getenv("STOCK_AGENT_LOOP_DUPLICATE_TOOL_LIMIT", "3")
    )
    agent_shutdown_grace_seconds: int = int(os.getenv("STOCK_AGENT_SHUTDOWN_GRACE_SECONDS", "90"))
    agent_run_max_attempts: int = int(os.getenv("STOCK_AGENT_RUN_MAX_ATTEMPTS", "3"))
    agent_worker_concurrency: int = int(os.getenv("STOCK_AGENT_WORKER_CONCURRENCY", "4"))
    agent_team_max_members: int = int(os.getenv("STOCK_AGENT_TEAM_MAX_MEMBERS", "20"))
    agent_team_max_parallel: int = int(os.getenv("STOCK_AGENT_TEAM_MAX_PARALLEL", "4"))
    agent_team_timeout_seconds: int = int(os.getenv("STOCK_AGENT_TEAM_TIMEOUT_SECONDS", "600"))
    agent_user_active_limit: int = int(os.getenv("STOCK_AGENT_USER_ACTIVE_LIMIT", "2"))
    agent_queue_limit: int = int(os.getenv("STOCK_AGENT_QUEUE_LIMIT", "500"))
    agent_lock_ttl_seconds: int = int(os.getenv("STOCK_AGENT_LOCK_TTL_SECONDS", "60"))
    agent_event_retention_days: int = int(os.getenv("STOCK_AGENT_EVENT_RETENTION_DAYS", "14"))
    agent_event_flush_chars: int = int(os.getenv("STOCK_AGENT_EVENT_FLUSH_CHARS", "320"))
    tool_worker_concurrency: int = int(os.getenv("STOCK_TOOL_WORKER_CONCURRENCY", "4"))
    tool_external_execution_enabled: bool = os.getenv(
        "STOCK_TOOL_EXTERNAL_EXECUTION_ENABLED",
        "true" if os.getenv("STOCK_AGENT_REDIS_URL", "").strip() else "false",
    ).lower() == "true"
    tool_worker_lease_seconds: int = int(os.getenv("STOCK_TOOL_WORKER_LEASE_SECONDS", "60"))
    tool_worker_poll_seconds: float = float(os.getenv("STOCK_TOOL_WORKER_POLL_SECONDS", "0.5"))
    tool_result_wait_seconds: int = int(os.getenv("STOCK_TOOL_RESULT_WAIT_SECONDS", "620"))
    tool_stream_key: str = os.getenv("STOCK_TOOL_STREAM_KEY", "hermes:agent:tools")
    tool_dead_stream_key: str = os.getenv("STOCK_TOOL_DEAD_STREAM_KEY", "hermes:agent:tools:dead")
    tool_consumer_group: str = os.getenv("STOCK_TOOL_CONSUMER_GROUP", "hermes-tool-workers")
    artifact_version_limit: int = int(os.getenv("STOCK_ARTIFACT_VERSION_LIMIT", "50"))
    # 工作区根目录；空=默认 backend/agent_workspace。生产应配到 shared/ 下避免随 release 替换蒸发
    workspace_dir: str = os.getenv("STOCK_WORKSPACE_DIR", "")
    # 不硬编码默认管理员凭据；只有显式配置 STOCK_ADMIN_USERNAME + STOCK_ADMIN_PASSWORD 时才会创建初始账号
    admin_username: str = os.getenv("STOCK_ADMIN_USERNAME", "")
    admin_password: str = os.getenv("STOCK_ADMIN_PASSWORD", "")
    scheduler_enabled: bool = os.getenv("STOCK_SCHEDULER_ENABLED", "true").lower() == "true"
    feishu_bot_enabled: bool = os.getenv("STOCK_FEISHU_BOT_ENABLED", "true").lower() == "true"
    background_services_enabled: bool = os.getenv("STOCK_BACKGROUND_SERVICES_ENABLED", "true").lower() == "true"
    # 操作日志中间件：记录 /api/* 下的写操作；高频场景可关闭
    operation_log_enabled: bool = os.getenv("STOCK_OPERATION_LOG_ENABLED", "true").lower() == "true"
    log_level: str = os.getenv("STOCK_LOG_LEVEL", "INFO")
    log_dir: str = os.getenv("STOCK_LOG_DIR", "logs")
    log_retention_days: int = int(os.getenv("STOCK_LOG_RETENTION_DAYS", "90"))
    scheduler_tick_seconds: int = int(os.getenv("STOCK_SCHEDULER_TICK_SECONDS", "30"))
    scheduler_max_concurrent_jobs: int = int(
        os.getenv("STOCK_SCHEDULER_MAX_CONCURRENT_JOBS", "2")
    )
    scheduler_shutdown_grace_seconds: int = int(
        os.getenv("STOCK_SCHEDULER_SHUTDOWN_GRACE_SECONDS", "60")
    )
    stock_holidays: tuple[str, ...] = tuple(
        day.strip()
        for day in os.getenv("STOCK_HOLIDAYS", "").split(",")
        if day.strip()
    )
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("STOCK_CORS_ORIGINS", "*").split(",")
        if origin.strip()
    )
    mcp_allowed_hosts: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv(
            "STOCK_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,[::1]:*,shijingxuan.com"
        ).split(",") if value.strip()
    )
    mcp_allowed_origins: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv(
            "STOCK_MCP_ALLOWED_ORIGINS",
            "http://localhost:*,http://127.0.0.1:*,http://[::1]:*,https://shijingxuan.com",
        ).split(",") if value.strip()
    )

    @property
    def database_url(self) -> str:
        explicit = os.getenv("DATABASE_URL")
        if explicit:
            return explicit

        password = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            "?charset=utf8mb4"
        )

    @property
    def server_database_url(self) -> str:
        password = quote_plus(self.db_password)
        return (
            f"mysql+pymysql://{self.db_user}:{password}"
            f"@{self.db_host}:{self.db_port}/?charset=utf8mb4"
        )


settings = Settings()
