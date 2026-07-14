"""Embedding Service — 外部 API 封装。

决策（见 PROJECT_PLAN.md §3.2）：
  - 用外部 embedding API（GLM/OpenAI 兼容）而非 ChromaDB 默认 onnxruntime 本地模型
  - 原因：Render 免费实例 512MB RAM，onnxruntime 会拖垮内存与冷启动
  - ChromaDB 只存我们算好的向量，职责收敛为「向量存储 + 相似度检索」

特性：
  - embed_batch(texts) → list[list[float]]：批量请求 + 自动分片 + 失败重试
  - OpenAI 兼容协议，与现有 LLM 客户端同源，配置统一
  - 进程内 LRU 缓存（可选），降本
"""

import asyncio
import hashlib
import logging
from typing import Any

from openai import AsyncOpenAI

from app.utils.config import (
    OPENAI_EMBEDDING_API_KEY,
    OPENAI_EMBEDDING_BASE_URL,
    OPENAI_EMBEDDING_MODEL,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MAX_RETRIES,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton embedding client（与 llm.py 保持一致的模式）
# ---------------------------------------------------------------------------

_embedding_client: AsyncOpenAI | None = None


def get_embedding_client() -> AsyncOpenAI:
    """Get the singleton embedding client."""
    global _embedding_client
    if _embedding_client is None:
        _embedding_client = AsyncOpenAI(
            api_key=OPENAI_EMBEDDING_API_KEY,
            base_url=OPENAI_EMBEDDING_BASE_URL,
        )
    return _embedding_client


# ---------------------------------------------------------------------------
# Optional LRU embedding cache（进程内，降本）
# ---------------------------------------------------------------------------

_cache: dict[str, list[float]] = {}
_CACHE_MAX_SIZE = 500  # 避免无限膨胀


def _cache_key(text: str) -> str:
    """Generate a deterministic cache key for a text string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


async def embed_single(text: str) -> list[float]:
    """Embed a single text string via the external API.

    内部带重试逻辑，单条失败抛异常让上层处理。
    """
    client = get_embedding_client()

    last_error: Exception | None = None
    for attempt in range(EMBEDDING_MAX_RETRIES):
        try:
            response = await client.embeddings.create(
                model=OPENAI_EMBEDDING_MODEL,
                input=[text],
            )
            return list(response.data[0].embedding)

        except Exception as e:
            last_error = e
            if attempt < EMBEDDING_MAX_RETRIES - 1:
                wait = 2 ** attempt  # 指数退避：1s, 2s, 4s...
                logger.warning(
                    "Embedding API attempt %d/%d failed, retrying in %ds: %s",
                    attempt + 1, EMBEDDING_MAX_RETRIES, wait, e,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Embedding API failed all %d attempts: %s",
                    EMBEDDING_MAX_RETRIES, e,
                )

    raise RuntimeError(
        f"Embedding API 调用失败（{EMBEDDING_MAX_RETRIES} 次重试后）: {last_error}"
    )


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via the external embedding API.

    自动分片（按 EMBEDDING_BATCH_SIZE）、并发请求、带重试。
    使用 LRU 缓存：相同文本不重复调 API（单次研报内命中率高）。

    Args:
        texts: List of text strings to embed.

    Returns:
        List of embedding vectors, same order as input.
    """
    if not texts:
        return []

    # Step 1: 查缓存，找出 miss 的文本
    results: list[list[float] | None] = [None] * len(texts)
    miss_indices: list[int] = []
    miss_texts: list[str] = []

    for i, text in enumerate(texts):
        key = _cache_key(text)
        if key in _cache:
            results[i] = _cache[key]
        else:
            miss_indices.append(i)
            miss_texts.append(text)

    if not miss_texts:
        logger.debug("Embedding cache hit: %d/%d", len(texts), len(texts))
        # 全部命中，类型安全返回
        return [r for r in results if r is not None]  # Never actually None here

    logger.debug(
        "Embedding cache: %d hit / %d miss (total %d)",
        len(texts) - len(miss_texts), len(miss_texts), len(texts),
    )

    # Step 2: 分片请求 embedding API
    all_embeds: list[list[float]] = []
    batch_size = EMBEDDING_BATCH_SIZE

    for offset in range(0, len(miss_texts), batch_size):
        chunk = miss_texts[offset : offset + batch_size]

        last_error: Exception | None = None
        for attempt in range(EMBEDDING_MAX_RETRIES):
            try:
                client = get_embedding_client()
                response = await client.embeddings.create(
                    model=OPENAI_EMBEDDING_MODEL,
                    input=chunk,
                )
                # 按 index 排序保证顺序
                sorted_data = sorted(response.data, key=lambda d: d.index)
                chunk_embeds = [list(d.embedding) for d in sorted_data]
                all_embeds.extend(chunk_embeds)
                break  # Success — exit retry loop

            except Exception as e:
                last_error = e
                if attempt < EMBEDDING_MAX_RETRIES - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Embedding batch API attempt %d/%d failed (offset=%d, size=%d), "
                        "retrying in %ds: %s",
                        attempt + 1, EMBEDDING_MAX_RETRIES, offset, len(chunk), wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "Embedding batch API failed all %d attempts (offset=%d): %s",
                        EMBEDDING_MAX_RETRIES, offset, e,
                    )
                    raise RuntimeError(
                        f"Embedding API 批量调用失败（{EMBEDDING_MAX_RETRIES} 次重试后）: {last_error}"
                    )

    # Step 3: 写回缓存并填 results
    for j, idx in enumerate(miss_indices):
        embed = all_embeds[j]
        text = miss_texts[j]
        key = _cache_key(text)

        # 简单的 LRU 驱逐：超限时清一半
        if len(_cache) >= _CACHE_MAX_SIZE:
            # 删掉最旧的半数条目
            keys_to_remove = list(_cache.keys())[:_CACHE_MAX_SIZE // 2]
            for k in keys_to_remove:
                del _cache[k]
            logger.debug("Embedding cache pruned %d entries", len(keys_to_remove))

        _cache[key] = embed
        results[idx] = embed

    # 类型安全：此时 results 全部非 None
    return [r for r in results if r is not None]  # type: ignore[return-value]
