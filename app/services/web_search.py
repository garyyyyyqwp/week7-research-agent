"""Web Search — Tavily API integration.

Replaces the Week 4/5 Mock search_web with real internet search.
Tavily is purpose-built for AI agents: returns structured results with
title, url, content (snippet), and published_date.

Fallback: Jina Reader (zero-config, GET https://s.jina.ai/{query}).
"""

import logging
from typing import Any

from app.utils.config import TAVILY_API_KEY

logger = logging.getLogger(__name__)


async def search_web(
    query: str,
    num_results: int = 5,
    include_raw_content: bool = False,
) -> list[dict[str, Any]]:
    """Search the web using Tavily Search API.

    Args:
        query: Search query string.
        num_results: Number of results to return (max 10 for Tavily free tier).
        include_raw_content: If True, also fetch raw page content (costs extra).

    Returns:
        List of structured results, each with:
          - title: Article title
          - url: Article URL
          - snippet: Short text summary
          - published_date: Publication date (if available)
          - raw_content: Full page text (only if include_raw_content=True)
    """
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=min(num_results, 10),
            include_raw_content=include_raw_content,
        )

        results: list[dict[str, Any]] = []
        for r in response.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "published_date": r.get("published_date", ""),
                "score": r.get("score", None),
            })

        logger.info("Tavily search: query='%s' → %d results", query, len(results))
        return results

    except Exception as e:
        logger.warning("Tavily search failed: %s, falling back to Jina", e)
        return await _search_via_jina(query, num_results)


async def _search_via_jina(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """Fallback: search via Jina Reader (zero-config, no API key).

    Jina's s.jina.ai endpoint returns search results as clean text.
    """
    import httpx

    url = f"https://s.jina.ai/{query}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={
                "Accept": "application/json",
                "X-Return-Format": "markdown",
            })
            resp.raise_for_status()
            data = resp.json()

        results: list[dict[str, Any]] = []
        for r in data.get("data", [])[:num_results]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:500],
                "published_date": r.get("published_date", ""),
                "score": None,
            })

        logger.info("Jina search: query='%s' → %d results", query, len(results))
        return results

    except Exception as e:
        logger.error("Jina search also failed: %s", e)
        return [{
            "title": "搜索失败",
            "url": "",
            "snippet": f"搜索服务暂时不可用: {str(e)}",
            "published_date": "",
            "score": None,
        }]


# ---------------------------------------------------------------------------
# Mock DB for comparison testing (保留 Week 5 的 Mock 实现用于对比)
# ---------------------------------------------------------------------------

async def search_web_mock(query: str, num_results: int = 5) -> list[dict[str, Any]]:
    """Mock web search — Week 5 版本，用于对比测试。"""
    mock_db: dict[str, dict] = {
        "深度学习": {
            "title": "深度学习（Deep Learning）概述",
            "url": "https://zh.wikipedia.org/wiki/深度学习",
            "snippet": "深度学习是机器学习的一个分支，基于人工神经网络的研究。大语言模型、扩散模型、多模态模型等是近年主要进展。",
            "published_date": "2024-01-01",
        },
        "人工智能": {
            "title": "人工智能（Artificial Intelligence）",
            "url": "https://zh.wikipedia.org/wiki/人工智能",
            "snippet": "人工智能是计算机科学的一个分支，致力于创建能够执行通常需要人类智能的任务的系统。2024-2025年生成式AI成为最热门方向。",
            "published_date": "2024-06-01",
        },
        "transformer": {
            "title": "Transformer — Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "snippet": "Transformer是一种基于自注意力机制的深度学习架构，由Vaswani等人在2017年提出。它是GPT、BERT等模型的基础架构。",
            "published_date": "2017-06-12",
        },
        "python": {
            "title": "Python Programming Language",
            "url": "https://www.python.org/",
            "snippet": "Python是一种高级编程语言，由Guido van Rossum于1991年首次发布。它是当前AI和数据科学领域最流行的编程语言。",
            "published_date": "1991-02-20",
        },
    }

    results = []
    query_lower = query.lower()
    for key, value in mock_db.items():
        if key.lower() in query_lower:
            results.append(value)
            if len(results) >= num_results:
                break

    if not results:
        results.append({
            "title": f"搜索结果: {query}",
            "url": f"https://example.com/search?q={query}",
            "snippet": f"根据网络搜索结果，以下是与「{query}」相关的信息。这是一个开放性话题，网络上存在大量相关讨论。",
            "published_date": "",
        })

    return results
