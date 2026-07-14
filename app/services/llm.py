"""LLM Client — OpenAI-compatible async client."""

from openai import AsyncOpenAI

from app.utils.config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Get the singleton LLM client."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    return _client


def get_model() -> str:
    """Get the configured model name."""
    return OPENAI_MODEL
