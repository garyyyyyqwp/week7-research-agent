"""LLM Client — OpenAI-compatible async client."""

import logging

import httpx
from openai import AsyncOpenAI

from app.utils.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Get the singleton LLM client.

    显式超时：跨境链路（Render Singapore → cn-beijing MaaS）挂起时，
    SDK 默认 600s×重试会把大纲/摘要阶段冻结最长 ~30 分钟且无任何事件；
    read=120s 对流式响应意味着"两个 chunk 之间最多等 120s"，正常生成不受影响。
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=30.0),
            max_retries=1,
        )
        logger.info("LLM client init: base=%s model=%s", OPENAI_BASE_URL, OPENAI_MODEL)
    return _client


def get_model() -> str:
    """Get the configured model name."""
    return OPENAI_MODEL
