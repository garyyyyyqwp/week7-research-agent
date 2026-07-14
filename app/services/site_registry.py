"""Site Registry — 定向站点抓取，研报平台的核心差异化功能。

Allows the agent to search specific authoritative sites (PubMed, arXiv, WHO, etc.)
directly, bypassing generic search engines for higher-quality results.

Supports two fetch strategies per site:
  - "api": The site has an official API (e.g., PubMed Entrez, arXiv API).
  - "jina": Use Jina Reader to extract results from the site's search page.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Site Configuration
# ---------------------------------------------------------------------------

@dataclass
class SiteConfig:
    """Configuration for a searchable site."""
    name: str                          # Human-readable name
    category: str                      # "medical", "academic", "tech", etc.
    search_url: str                    # URL template with {query} placeholder
    fetch_strategy: str                # "api" or "jina"
    api_module: str | None = None      # Module path for API strategy
    description: str = ""              # Short description of the site
    rate_limit: float = 0.5            # Seconds between requests


# ---------------------------------------------------------------------------
# Site Registry
# ---------------------------------------------------------------------------


SITE_REGISTRY: dict[str, SiteConfig] = {
    # --- Medical ---
    "pubmed": SiteConfig(
        name="PubMed",
        category="medical",
        search_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                   "db=pubmed&retmax={num_results}&retmode=json&term={query}",
        fetch_strategy="api",
        api_module="entrez",
        description="美国国家医学图书馆的生物医学文献数据库",
        rate_limit=0.34,  # NCBI allows ~3/sec without API key
    ),
    "who": SiteConfig(
        name="WHO",
        category="medical",
        search_url="https://www.who.int/search?query={query}",
        fetch_strategy="jina",
        description="世界卫生组织官方网站",
        rate_limit=0.5,
    ),
    "cdc": SiteConfig(
        name="CDC",
        category="medical",
        search_url="https://www.cdc.gov/search/index.html?query={query}",
        fetch_strategy="jina",
        description="美国疾病控制与预防中心",
        rate_limit=0.5,
    ),

    # --- Academic ---
    "arxiv": SiteConfig(
        name="arXiv",
        category="academic",
        search_url="http://export.arxiv.org/api/query?"
                   "search_query=all:{query}&start=0&max_results={num_results}",
        fetch_strategy="api",
        api_module="arxiv",
        description="预印本论文库（物理、数学、CS、AI等领域）",
        rate_limit=0.5,
    ),
    "semantic_scholar": SiteConfig(
        name="Semantic Scholar",
        category="academic",
        search_url="https://api.semanticscholar.org/graph/v1/paper/search?"
                   "query={query}&limit={num_results}",
        fetch_strategy="api",
        api_module="semantic_scholar",
        description="AI驱动的学术论文搜索引擎",
        rate_limit=1.0,
    ),

    # --- Tech ---
    "github": SiteConfig(
        name="GitHub",
        category="tech",
        search_url="https://github.com/search?q={query}&type=repositories",
        fetch_strategy="jina",
        description="开源代码托管平台",
        rate_limit=0.5,
    ),
}

# Rate limiting state: {site_id: last_request_timestamp}
_last_request: dict[str, float] = {}


async def _enforce_rate_limit(site_id: str, config: SiteConfig):
    """Sleep if needed to respect per-site rate limits (non-blocking)."""
    now = time.monotonic()
    last = _last_request.get(site_id, 0)
    elapsed = now - last
    if elapsed < config.rate_limit:
        await asyncio.sleep(config.rate_limit - elapsed)
    _last_request[site_id] = time.monotonic()


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


async def search_site(
    site_id: str,
    query: str,
    num_results: int = 5,
) -> dict[str, Any]:
    """Search a specific site from the registry.

    Args:
        site_id: Key in SITE_REGISTRY (e.g., "pubmed", "arxiv", "who").
        query: Search query.
        num_results: Number of results to return.

    Returns:
        Dict with:
          - site_id, site_name, category
          - results: list of {title, url, snippet, published_date}
          - strategy: which fetch strategy was used
          - error: error message if any
    """
    config = SITE_REGISTRY.get(site_id)
    if not config:
        available = ", ".join(SITE_REGISTRY.keys())
        return {
            "site_id": site_id,
            "site_name": "未知",
            "category": "unknown",
            "results": [],
            "strategy": "none",
            "error": f"未注册的站点 '{site_id}'。可用站点: {available}",
        }

    await _enforce_rate_limit(site_id, config)

    try:
        if config.fetch_strategy == "api":
            results = await _search_via_api(config, query, num_results)
        else:
            results = await _search_via_jina_extraction(config, query, num_results)

        logger.info(
            "search_site(%s): '%s' → %d results",
            site_id, query, len(results),
        )
        return {
            "site_id": site_id,
            "site_name": config.name,
            "category": config.category,
            "results": results,
            "strategy": config.fetch_strategy,
            "error": None,
        }

    except Exception as e:
        logger.error("search_site(%s) error: %s", site_id, e)
        return {
            "site_id": site_id,
            "site_name": config.name,
            "category": config.category,
            "results": [],
            "strategy": "error",
            "error": str(e),
        }


def list_sites(category: str | None = None) -> list[dict[str, str]]:
    """List all registered sites, optionally filtered by category.

    Args:
        category: Optional filter (e.g., "medical", "academic").

    Returns:
        List of {id, name, category, description} dicts.
    """
    result = []
    for site_id, config in SITE_REGISTRY.items():
        if category and config.category != category:
            continue
        result.append({
            "id": site_id,
            "name": config.name,
            "category": config.category,
            "description": config.description,
        })
    return result


# ---------------------------------------------------------------------------
# API-based search strategies
# ---------------------------------------------------------------------------


async def _search_via_api(
    config: SiteConfig,
    query: str,
    num_results: int,
) -> list[dict[str, Any]]:
    """Search via official API."""
    if config.api_module == "entrez":
        return await _search_pubmed(query, num_results)
    elif config.api_module == "arxiv":
        return await _search_arxiv(query, num_results)
    elif config.api_module == "semantic_scholar":
        return await _search_semantic_scholar(query, num_results)
    else:
        raise ValueError(f"Unknown API module: {config.api_module}")


async def _search_pubmed(query: str, num_results: int) -> list[dict[str, Any]]:
    """Search PubMed via NCBI Entrez API.

    Step 1: esearch to get PMIDs
    Step 2: efetch to get abstracts
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    # Step 1: Search
    async with httpx.AsyncClient(timeout=15.0) as client:
        search_url = (
            f"{base}/esearch.fcgi?db=pubmed&retmax={num_results}"
            f"&retmode=json&term={query}"
        )
        resp = await client.get(search_url)
        resp.raise_for_status()
        search_data = resp.json()
        pmids = search_data.get("esearchresult", {}).get("idlist", [])

        if not pmids:
            logger.info("PubMed: no results for '%s'", query)
            return []

        # Step 2: Fetch abstracts
        fetch_url = (
            f"{base}/efetch.fcgi?db=pubmed&retmode=xml&id={','.join(pmids)}"
        )
        resp = await client.get(fetch_url)
        resp.raise_for_status()
        xml_text = resp.text

    # Parse XML for title + abstract
    from xml.etree import ElementTree as ET
    root = ET.fromstring(xml_text)

    results = []
    for article in root.findall(".//PubmedArticle"):
        # Title
        title_elem = article.find(".//ArticleTitle")
        title = "".join(title_elem.itertext()) if title_elem is not None else "无标题"

        # Abstract
        abstract_parts = []
        for abs_elem in article.findall(".//AbstractText"):
            label = abs_elem.get("Label", "")
            text = "".join(abs_elem.itertext())
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        abstract = " ".join(abstract_parts)

        # PMID
        pmid_elem = article.find(".//PMID")
        pmid = "".join(pmid_elem.itertext()) if pmid_elem is not None else ""

        # Date
        date_elem = article.find(".//PubDate")
        date_str = ""
        if date_elem is not None:
            year = date_elem.findtext("Year", "")
            month = date_elem.findtext("Month", "")
            day = date_elem.findtext("Day", "")
            date_str = f"{year}-{month}-{day}".strip("-")

        results.append({
            "title": title.strip(),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "snippet": abstract.strip()[:500],
            "published_date": date_str,
            "pmid": pmid,
        })

    return results


async def _search_arxiv(query: str, num_results: int) -> list[dict[str, Any]]:
    """Search arXiv via their public API."""
    import re
    from xml.etree import ElementTree as ET

    url = (
        f"http://export.arxiv.org/api/query?"
        f"search_query=all:{query}&start=0&max_results={num_results}"
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        xml_text = resp.text

    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    results = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", "").strip().replace("\n", " ")
        summary = entry.findtext("atom:summary", "").strip().replace("\n", " ")
        arxiv_id = entry.findtext("atom:id", "").rsplit("/", 1)[-1]
        # Remove version number from ID for the URL
        arxiv_id_clean = re.sub(r'v\d+$', '', arxiv_id)
        published = entry.findtext("atom:published", "")[:10]

        results.append({
            "title": title,
            "url": f"https://arxiv.org/abs/{arxiv_id_clean}",
            "snippet": summary[:500],
            "published_date": published,
            "arxiv_id": arxiv_id,
        })

    return results


async def _search_semantic_scholar(query: str, num_results: int) -> list[dict[str, Any]]:
    """Search Semantic Scholar API."""
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/search?"
        f"query={query}&limit={num_results}&"
        f"fields=title,url,abstract,year,authors"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    results = []
    for paper in data.get("data", []):
        authors = ", ".join(
            a.get("name", "") for a in paper.get("authors", [])[:3]
        )
        snippet = paper.get("abstract", "")
        if authors:
            snippet = f"Authors: {authors}\n{snippet}" if snippet else f"Authors: {authors}"

        results.append({
            "title": paper.get("title", ""),
            "url": paper.get("url", f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}"),
            "snippet": snippet[:500],
            "published_date": str(paper.get("year", "")),
        })

    return results


# ---------------------------------------------------------------------------
# Jina-based extraction (for sites without APIs)
# ---------------------------------------------------------------------------


async def _search_via_jina_extraction(
    config: SiteConfig,
    query: str,
    num_results: int,
) -> list[dict[str, Any]]:
    """Use Jina Reader to extract results from a site's search page.

    This is the most practical approach for sites without APIs.
    Jina Reader renders the page and returns clean Markdown.
    """
    from app.services.content_fetcher import fetch_url

    # Construct search URL
    search_url = config.search_url.format(query=query, num_results=num_results)

    result = await fetch_url(search_url, max_chars=3000, strategy="jina")
    content = result.get("content", "")

    if not content or result.get("error"):
        return [{
            "title": f"{config.name} 搜索失败",
            "url": search_url,
            "snippet": f"无法从 {config.name} 获取内容: {result.get('error', 'Unknown error')}",
            "published_date": "",
        }]

    # Try to extract structured results from the markdown
    # Jina typically returns the page as clean text — we extract potential
    # result items by looking for patterns like headings followed by links
    items = _parse_jina_search_results(content, config.name)

    if not items:
        # Return the raw content as a single result
        return [{
            "title": f"{config.name} 搜索结果: {query}",
            "url": search_url,
            "snippet": content[:500],
            "published_date": "",
        }]

    return items[:num_results]


def _parse_jina_search_results(
    content: str,
    site_name: str,
) -> list[dict[str, Any]]:
    """Heuristic parsing of Jina Reader output into structured search results.

    Looks for markdown patterns like:
      ## Title
      [Title](url)
      snippet text...
    """
    import re

    # Find markdown links with surrounding context
    link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

    results = []
    lines = content.split("\n")

    for i, line in enumerate(lines):
        # Look for headings as result titles
        if line.startswith("## ") or line.startswith("### "):
            title = line.lstrip("# ").strip()
            # Skip if it looks like a section header, not a result
            if len(title) > 80 or any(
                kw in title.lower()
                for kw in ["search", "result", "footer", "header", "menu", "navigation", "cookie"]
            ):
                continue

            # Get following lines as snippet
            snippet_lines = []
            for j in range(i + 1, min(i + 4, len(lines))):
                l = lines[j].strip()
                if l and not l.startswith("##"):
                    snippet_lines.append(l)

            snippet = " ".join(snippet_lines)
            # Extract URL from the snippet
            urls = link_pattern.findall(snippet)
            url = urls[0][1] if urls else ""

            results.append({
                "title": title[:200],
                "url": url,
                "snippet": snippet[:500] if snippet else title,
                "published_date": "",
            })

    return results
