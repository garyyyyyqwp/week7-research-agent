"""Content Fetcher — extract clean article text from URLs.

Strategy:
  1. Jina Reader: GET https://r.jina.ai/{url} → clean Markdown (preferred)
  2. httpx + BeautifulSoup: fallback for when Jina is unavailable

Returns clean, truncated Markdown suitable for LLM context.

SSRF guard: validate_public_url() lives HERE (service layer) so that every
caller is protected — the router-level Pydantic validator only covers
POST /search/fetch, while the agent tool chain and research engine call
fetch_url() directly.
"""

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# --- SSRF blocklist（服务层统一守卫）---
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
]


def validate_public_url(url: str) -> str:
    """Validate URL is safe to fetch server-side: http/https only, no private IPs.

    Raises ValueError on unsafe URLs. Agent 工具链的 URL 来自 LLM 决策 +
    抓取页面内容（可被间接注入），必须在这里而不是仅在路由层校验。
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"不支持的协议: {parsed.scheme or '(无)'}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("无法解析URL中的主机名")
    if hostname.lower() in _BLOCKED_HOSTS:
        raise ValueError("不允许访问该主机")

    # IP 字面量直接检查内网段（域名解析级校验的成本/收益比不适合本项目规模）
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return url  # DNS 域名，放行
    for net in _BLOCKED_NETWORKS:
        if addr in net:
            raise ValueError("不允许访问内网地址")
    return url


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

    # SSRF 守卫：所有调用方（路由/Agent工具/研究引擎）统一在此拦截
    try:
        validate_public_url(url)
    except ValueError as e:
        logger.warning("fetch_url blocked unsafe URL %s: %s", url[:120], e)
        return {
            "url": url,
            "content": "",
            "full_length": 0,
            "strategy": "blocked",
            "error": f"URL 被安全策略拦截: {e}",
        }

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
