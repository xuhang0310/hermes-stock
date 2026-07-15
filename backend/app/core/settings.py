from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[2]  # core/ -> app/ -> backend/（backend/.env 所在）
# 仅在开发者本机可用作备用值源（存在才加载，不存在就跳过，不阻塞其他机器）
_RABBIT_ENV = Path(os.getenv("STOCK_RABBIT_ENV_PATH", ""))
if not _RABBIT_ENV.is_file():
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
    webhook_enc_key: str = os.getenv("STOCK_WEBHOOK_ENC_KEY", "")
    llm_base_url: str = os.getenv("STOCK_LLM_BASE_URL", os.getenv("RABBIT_LLM_BASE_URL", ""))
    llm_api_key: str = os.getenv("STOCK_LLM_API_KEY", os.getenv("RABBIT_LLM_API_KEY", ""))
    llm_model: str = os.getenv("STOCK_LLM_MODEL", os.getenv("RABBIT_LLM_MODEL", "gpt-4.1-mini"))
    agent_max_steps: int = int(os.getenv("STOCK_AGENT_MAX_STEPS", os.getenv("RABBIT_AGENT_MAX_STEPS", "6")))
    tushare_token: str = os.getenv("STOCK_TUSHARE_TOKEN", os.getenv("TUSHARE_TOKEN", ""))
    tavily_api_key: str = os.getenv("STOCK_TAVILY_API_KEY", os.getenv("TAVILY_API_KEY", ""))
    brave_api_key: str = os.getenv("STOCK_BRAVE_API_KEY", os.getenv("BRAVE_API_KEY", ""))
    # 向量记忆嵌入服务（与聊天模型解耦；未配置则回退活跃 LLM 提供商的 /embeddings）
    embedding_base_url: str = os.getenv("STOCK_EMBEDDING_BASE_URL", "")
    embedding_api_key: str = os.getenv("STOCK_EMBEDDING_API_KEY", "")
    embedding_model: str = os.getenv("STOCK_EMBEDDING_MODEL", "")
    # 记忆检索重排模型（复用 embedding 的 base_url/api_key，走同一提供商的 /rerank）；空=关闭重排
    rerank_model: str = os.getenv("STOCK_RERANK_MODEL", "")
    # 向量库后端：chroma（嵌入式，默认，本地/Windows）| milvus（服务端）
    vector_backend: str = os.getenv("STOCK_VECTOR_BACKEND", "chroma")
    milvus_uri: str = os.getenv("STOCK_MILVUS_URI", "http://127.0.0.1:19530")
    milvus_token: str = os.getenv("STOCK_MILVUS_TOKEN", "")
    # RAG 知识库（P1）：切块/检索参数；向量后端沿用 vector_backend/milvus_uri，collection 独立
    rag_enabled: bool = os.getenv("STOCK_RAG_ENABLED", "true").lower() == "true"
    rag_milvus_collection: str = os.getenv("STOCK_RAG_MILVUS_COLLECTION", "knowledge_chunks")
    rag_top_k: int = int(os.getenv("STOCK_RAG_TOP_K", "6"))
    rag_chunk_size: int = int(os.getenv("STOCK_RAG_CHUNK_SIZE", "700"))
    rag_chunk_overlap: int = int(os.getenv("STOCK_RAG_OVERLAP", "80"))
    rag_min_score: float = float(os.getenv("STOCK_RAG_MIN_SCORE", "0.35"))  # P2 注入阈值，先建
    # 不硬编码默认管理员凭据；只有显式配置 STOCK_ADMIN_USERNAME + STOCK_ADMIN_PASSWORD 时才会创建初始账号
    admin_username: str = os.getenv("STOCK_ADMIN_USERNAME", "")
    admin_password: str = os.getenv("STOCK_ADMIN_PASSWORD", "")
    scheduler_enabled: bool = os.getenv("STOCK_SCHEDULER_ENABLED", "true").lower() == "true"
    feishu_bot_enabled: bool = os.getenv("STOCK_FEISHU_BOT_ENABLED", "true").lower() == "true"
    log_level: str = os.getenv("STOCK_LOG_LEVEL", "INFO")
    log_dir: str = os.getenv("STOCK_LOG_DIR", "logs")
    log_retention_days: int = int(os.getenv("STOCK_LOG_RETENTION_DAYS", "90"))
    scheduler_tick_seconds: int = int(os.getenv("STOCK_SCHEDULER_TICK_SECONDS", "30"))
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
