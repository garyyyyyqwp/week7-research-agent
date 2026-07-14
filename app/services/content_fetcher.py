"""Content Fetcher — extract clean article text from URLs.

Strategy:
  1. Jina Reader: GET https://r.jina.ai/{url} → clean Markdown (preferred)
  2. httpx + BeautifulSoup: fallback for when Jina is unavailable

Returns clean, truncated Markdown suitable for LLM context.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def fetch_url(
    url: str,
    max_chars: int = 3000,
    strategy: str = "jina",
) -> dict[str, Any]:
    """Fetch and extract clean article text from a URL.

    Args:
        url: The webpage URL to fetch.
        max_chars: Maximum characters to return (prevents token overflow).
        strategy: "jina" (default) or "bs4" for fallback.

    Returns:
        Dict with:
          - url: Original URL
          - content: Clean Markdown text, truncated to max_chars
          - full_length: Original character count before truncation
          - strategy: Which strategy was used
          - error: Error message (if fetch failed)
    """
    content = ""
    error = None
    used_strategy = strategy

    try:
        if strategy == "jina":
            content, used_strategy = await _fetch_via_jina(url)
            if not content:
                logger.warning("Jina returned empty for %s, trying bs4 fallback", url)
                content, used_strategy = await _fetch_via_bs4(url)
        else:
            content, used_strategy = await _fetch_via_bs4(url)
    except Exception as e:
        error = str(e)
        logger.error("fetch_url error for %s: %s", url, e)
        # Try bs4 fallback if Jina failed
        if strategy == "jina":
            try:
                content, used_strategy = await _fetch_via_bs4(url)
                error = None
            except Exception as e2:
                logger.error("bs4 fallback also failed: %s", e2)

    full_length = len(content)

    # Truncate to max_chars, trying to break at a sentence boundary
    if len(content) > max_chars:
        truncated = content[:max_chars]
        # Try to break at the last complete sentence
        for sep in [". ", "。", "\n\n", "\n", " "]:
            last_idx = truncated.rfind(sep)
            if last_idx > max_chars * 0.7:
                truncated = truncated[:last_idx + len(sep)]
                break
        content = truncated.strip()

    return {
        "url": url,
        "content": content,
        "full_length": full_length,
        "strategy": used_strategy,
        "error": error,
    }


async def _fetch_via_jina(url: str) -> tuple[str, str]:
    """Fetch clean content via Jina Reader API.

    Returns:
        (content, "jina") tuple.
    """
    jina_url = f"https://r.jina.ai/{url}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(jina_url, headers={
            "Accept": "text/markdown",
        })
        resp.raise_for_status()
        content = resp.text

    # Jina prepends a title line — keep it, it's useful context
    logger.info("Jina fetched %d chars from %s", len(content), url)
    return content, "jina"


async def _fetch_via_bs4(url: str) -> tuple[str, str]:
    """Fetch and extract content using httpx + BeautifulSoup.

    Returns:
        (content, "bs4") tuple.
    """
    from bs4 import BeautifulSoup

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        })
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "lxml")

    # Remove noise elements
    for tag in soup.select(
        "script, style, nav, footer, header, aside, "
        ".sidebar, .ad, .advertisement, .nav, .menu, "
        '[role="navigation"], [role="banner"]'
    ):
        tag.decompose()

    # Prefer <article> or <main>, fall back to <body>
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find("body")
    )

    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Clean up: collapse multiple newlines
    import re
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    logger.info("bs4 extracted %d chars from %s", len(text), url)
    return text, "bs4"
