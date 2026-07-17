"""Configuration — environment variable loading.

Week 7: 研究报告智能体 — 配置扩展了 embedding、ResearchContext、管道参数。
"""

import os
from dotenv import load_dotenv

load_dotenv()


def get_env(key: str, default: str | None = None, required: bool = False) -> str:
    """Get environment variable with optional validation."""
    value = os.getenv(key, default)
    if required and value is None:
        raise ValueError(
            f"Environment variable '{key}' is not set. "
            f"Please set it in your .env file or system environment."
        )
    return value


# --- LLM ---
# 默认值与 .env.example / render.yaml 保持同一供应商（阿里云 MaaS + Qwen），
# 避免"智谱 URL + Qwen 模型名"的错配：那会让服务启动正常、健康检查通过，
# 但所有 LLM 调用 100% 失败
_DEFAULT_BASE_URL = "https://ws-m641g6tn4koc8942.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

OPENAI_API_KEY: str = get_env("OPENAI_API_KEY", required=True)
OPENAI_BASE_URL: str = get_env("OPENAI_BASE_URL", _DEFAULT_BASE_URL)
OPENAI_MODEL: str = get_env("OPENAI_MODEL", "qwen3-235b-a22b-instruct-2507")

# --- Embedding（新增 Week 7）---
# 用外部 embedding API 而非 ChromaDB 默认本地 onnxruntime 模型，避免拖垮 Render 内存与冷启动
OPENAI_EMBEDDING_BASE_URL: str = get_env(
    "OPENAI_EMBEDDING_BASE_URL",
    get_env("OPENAI_BASE_URL", _DEFAULT_BASE_URL),
)
# 空字符串视为未配置 → 回退主 API KEY（Render 面板存了空值也不至于全部 401）
OPENAI_EMBEDDING_API_KEY: str = (
    get_env("OPENAI_EMBEDDING_API_KEY", "") or get_env("OPENAI_API_KEY", "")
)
OPENAI_EMBEDDING_MODEL: str = get_env("OPENAI_EMBEDDING_MODEL", "text-embedding-v3")

# --- Tavily Search ---
TAVILY_API_KEY: str = get_env("TAVILY_API_KEY", required=True)

# --- Agent ---
AGENT_MAX_STEPS: int = int(get_env("AGENT_MAX_STEPS", "10"))

# --- Pipeline Parameters（新增 Week 7）---
RETRIEVE_TOP_K: int = int(get_env("RETRIEVE_TOP_K", "8"))
CHUNK_TARGET_TOKENS: int = int(get_env("CHUNK_TARGET_TOKENS", "300"))
CHUNK_OVERLAP: float = float(get_env("CHUNK_OVERLAP", "0.15"))
MIN_SOURCES: int = int(get_env("MIN_SOURCES", "3"))
PER_SEARCH_TIMEOUT: int = int(get_env("PER_SEARCH_TIMEOUT", "15"))
EMBEDDING_BATCH_SIZE: int = int(get_env("EMBEDDING_BATCH_SIZE", "10"))  # 阿里云 text-embedding-v3 批量上限 10, >10 必 400
EMBEDDING_MAX_RETRIES: int = int(get_env("EMBEDDING_MAX_RETRIES", "3"))
