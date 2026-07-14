"""Agent Tools — Week 6 版本，含真实搜索 + 站点抓取 + 引用注册。

Tools are defined in OpenAI Function Calling format.
Each tool executor accepts an optional CitationManager and registers sources.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.services.web_search import search_web
from app.services.content_fetcher import fetch_url
from app.services.site_registry import search_site, list_sites

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "搜索互联网获取最新信息。返回结构化结果列表，每条含标题、URL、摘要和发布日期。"
                "适合查找行业趋势、新闻报道、事件进展等需要实时信息的场景。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询文本。建议使用具体的关键词或短语。中文和英文均支持。",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认5，最大10",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "获取并提取网页的正文内容（Markdown格式）。适合需要深入阅读某个具体网页全文的场景。"
                "返回干净的正文文字，自动去除广告和导航等噪声内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "目标网页的完整 URL",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大返回字符数，默认3000。正文过长时自动截断",
                        "default": 3000,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_site",
            "description": (
                "在指定的权威站点内搜索。支持: pubmed(医学文献), arxiv(预印本论文), "
                "semantic_scholar(学术), who(世界卫生组织), cdc(疾控中心), github(开源代码)。"
                "返回质量高于通用搜索引擎，因为数据来自权威机构的官方 API 或结构化页面。"
                "研报写出时优先使用此工具从权威来源获取信息。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "站点ID: pubmed/who/cdc/arxiv/semantic_scholar/github",
                        "enum": ["pubmed", "who", "cdc", "arxiv", "semantic_scholar", "github"],
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索查询文本",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认5，最大10",
                        "default": 5,
                    },
                },
                "required": ["site_id", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "安全计算数学表达式。支持加减乘除、乘方、取模和常量 pi、e。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，例如 '(365 * 8) / 12'",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。需要确认时间背景时使用。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Executors
# ---------------------------------------------------------------------------

async def execute_search_web(query: str, num_results: int = 5, citation_manager=None) -> str:
    """Search the web and register citations."""
    results = await search_web(query, num_results=num_results)

    # Register citations
    if citation_manager:
        for r in results:
            if r.get("url"):
                citation_manager.add(
                    url=r["url"],
                    title=r["title"],
                    snippet=r["snippet"],
                    source_type="web",
                )

    # Format results for the LLM
    if not results:
        return "未找到与查询相关的搜索结果。"

    parts = [f"[search_web 结果] 查询: {query}\n"]
    for i, r in enumerate(results, 1):
        parts.append(
            f"[{i}] {r['title']}\n"
            f"    URL: {r['url']}\n"
            f"    摘要: {r['snippet'][:300]}\n"
        )
        if r.get("published_date"):
            parts[-1] += f"    日期: {r['published_date']}\n"

    # Include citation reference
    if citation_manager:
        parts.append(f"\n📌 以上来源已注册为引用，编号 [{citation_manager.count - len(results) + 1}]-[{citation_manager.count}]")

    return "\n".join(parts)


async def execute_fetch_url(url: str, max_chars: int = 3000, citation_manager=None) -> str:
    """Fetch and extract article content, register citation."""
    result = await fetch_url(url, max_chars=max_chars)

    # Register citation
    if citation_manager and not result.get("error"):
        # Extract title from content (first line of Jina output)
        content_preview = result.get("content", "")
        title = content_preview.split("\n")[0].strip("# ").strip() or url
        citation_manager.add(
            url=url,
            title=title[:200],
            snippet=content_preview[:300],
            source_type="web",
        )

    if result.get("error"):
        return f"获取网页内容失败: {result['error']}"

    return (
        f"[fetch_url 结果]\n"
        f"URL: {url}\n"
        f"正文长度: {result['full_length']} 字符 (返回前 {max_chars} 字符)\n"
        f"提取方式: {result['strategy']}\n\n"
        f"{result['content']}"
    )


async def execute_search_site(
    site_id: str,
    query: str,
    num_results: int = 5,
    citation_manager=None,
) -> str:
    """Search a specific site and register citations.

    If the directed site returns no results or errors, automatically falls
    back to Tavily web search so the research phase never stops dead.
    """
    result = await search_site(site_id=site_id, query=query, num_results=num_results)

    # --- Fallback: site failed or empty → Tavily ---
    if result.get("error") or not result.get("results"):
        fail_reason = result.get("error") or "no results"
        logger.warning(
            "search_site(%s) failed/empty (%s), falling back to Tavily", site_id, fail_reason,
        )
        # Run Tavily with the same query + site context
        fallback_query = f"{query} site:{site_id}"
        try:
            from app.services.web_search import search_web
            web_results = await search_web(fallback_query, num_results=num_results)
        except Exception:
            web_results = []
        if web_results:
            if citation_manager:
                for r in web_results:
                    if r.get("url"):
                        citation_manager.add(
                            url=r["url"],
                            title=r["title"],
                            snippet=r["snippet"],
                            source_type="web",
                            site_name=result.get("site_name", site_id) + " (via Tavily)",
                        )
            parts = [f"[search_site 降级] {result.get('site_name', site_id)} 返回空，转为Tavily搜索\n查询: {query}\n"]
            for i, r in enumerate(web_results, 1):
                parts.append(f"[{i}] {r['title']}\n    URL: {r['url']}\n    摘要: {r.get('snippet', '')[:300]}\n")
            return "\n".join(parts)
        return f"站点搜索失败 [{site_id}]: {fail_reason}（Tavily 降级搜索也无结果）"

    # Register citations
    if citation_manager:
        for r in result.get("results", []):
            if r.get("url"):
                citation_manager.add(
                    url=r["url"],
                    title=r["title"],
                    snippet=r.get("snippet", ""),
                    source_type="academic" if result.get("category") == "academic" else "official",
                    site_name=result.get("site_name", ""),
                )

    if not result.get("results"):
        return f"[{result['site_name']}] 未找到与 '{query}' 相关的结果。"

    if not result.get("results"):
        return f"[{result['site_name']}] 未找到与 '{query}' 相关的结果。"

    parts = [
        f"[search_site 结果] 站点: {result['site_name']} ({result['category']})\n"
        f"查询: {query}\n"
    ]
    for i, r in enumerate(result.get("results", []), 1):
        parts.append(
            f"[{i}] {r['title']}\n"
            f"    URL: {r['url']}\n"
            f"    摘要: {r['snippet'][:300]}\n"
        )

    if citation_manager:
        n = len(result.get("results", []))
        parts.append(f"\n📌 以上来源已注册为引用，编号 [{citation_manager.count - n + 1}]-[{citation_manager.count}]")

    return "\n".join(parts)


async def execute_calculator(expression: str) -> str:
    """Safe math expression evaluator."""
    import ast
    import operator as op

    _SAFE_OPS = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul,
        ast.Div: op.truediv, ast.Pow: op.pow, ast.Mod: op.mod,
        ast.USub: op.neg, ast.UAdd: op.pos,
    }
    _CONSTANTS = {"pi": 3.141592653589793, "e": 2.718281828459045}

    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"Unsupported expression: {type(node).__name__}")

    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree.body)
        return f"[计算结果] {expression} = {result}"
    except Exception as e:
        return f"计算错误: {str(e)}"


async def execute_get_current_time() -> str:
    """Get current date and time."""
    now = datetime.now(timezone.utc)
    return (
        f"[当前时间]\n"
        f"UTC: {now.isoformat()}\n"
        f"日期: {now.strftime('%Y年%m月%d日')}\n"
        f"时间: {now.strftime('%H:%M:%S')}"
    )


# ---------------------------------------------------------------------------
# Tool Dispatcher
# ---------------------------------------------------------------------------

_TOOLS: dict[str, Any] = {
    "search_web": execute_search_web,
    "fetch_url": execute_fetch_url,
    "search_site": execute_search_site,
    "calculator": execute_calculator,
    "get_current_time": execute_get_current_time,
}


async def execute_tool(tool_name: str, tool_args: dict[str, Any], citation_manager=None) -> str:
    """Execute a tool by name. Passes citation_manager for source tracking."""
    executor = _TOOLS.get(tool_name)
    if executor is None:
        return f"错误：未知工具 '{tool_name}'。可用工具: {', '.join(_TOOLS)}"

    try:
        return str(await executor(**tool_args, citation_manager=citation_manager))
    except TypeError:
        # Some tools (calculator, get_current_time) don't accept citation_manager
        return str(await executor(**tool_args))
    except Exception as e:
        logger.error("Tool error (%s): %s", tool_name, e)
        return f"工具执行错误 ({tool_name}): {e}"
