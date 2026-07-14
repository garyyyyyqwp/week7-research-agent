"""Research Engine — ReAct agent loop refactored for the research pipeline.

Refactored from agent.py's run_agent_stream() to be a composable component that:
  1. Runs parallel-tool-call ReAct loop for research
  2. Captures FULL text of every source into ResearchContext (for per-section RAG)
  3. Metadata goes into CitationManager (already done by agent_tools)
  4. Yields research_progress events for the SSE stream

Key differences from run_agent_stream():
  - No final "answer" — this is Phase 1 only, not a full agent session
  - Full-text capture: after each search/fetch, fetches complete article text
    and stores it in ResearchContext (solving Week 6's "丢弃全文" problem)
  - Configurable max_steps with graceful exit
  - Per-source timeout (PER_SEARCH_TIMEOUT)

See PROJECT_PLAN.md §5.2 for detailed design.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.services.llm import get_client, get_model
from app.services.agent_tools import (
    TOOL_DEFINITIONS,
    execute_tool,
)
from app.services.citation_manager import CitationManager
from app.services.research_context import ResearchContext
from app.services.content_fetcher import fetch_url as fetch_url_content
from app.utils.config import AGENT_MAX_STEPS, PER_SEARCH_TIMEOUT

logger = logging.getLogger(__name__)

# System prompt optimized for research data collection (Phase 1 only).
# Emphasis on parallel search, diverse sources, and deep fetching.
RESEARCH_SYSTEM_PROMPT = """你是一个AI研究助手，负责为研究报告收集信息。

你的能力:
- search_web: 搜索互联网获取最新信息
- fetch_url: 获取网页正文进行深度阅读
- search_site: 在权威站点（PubMed/arXiv/WHO/CDC等）内搜索

工作流程:
1. 分析研究主题，确定需要覆盖的信息维度
2. **第一步就同时发起多个搜索**，并行搜索不同站点和查询角度
3. 仔细阅读搜索结果，对关键文章用 fetch_url 获取完整正文
4. 确保收集的信息覆盖主题的各个关键方面

重要规则:
- 基于工具返回的实际结果来决策，不要编造
- **第一次行动就同时发起 3-5 个搜索**（如同时搜索 PubMed + WHO + 通用搜索），覆盖不同角度
- **至少完成 2 轮搜索**才能停止，不要仅搜索一次就结束
- 不要重复抓取同一个 URL
- 回复使用中文，专业术语可保留英文
- 当认为收集的信息足够覆盖主题的关键方面后，不再调用工具（系统会检测并进入下一阶段）"""


class ResearchEngine:
    """Research Phase engine — runs ReAct loop to collect sources.

    Designed as a composable component consumed by ResearchPipeline (Phase 1).
    Each instance is tied to one research session.

    Usage:
        engine = ResearchEngine(cm, rc, max_steps=10)
        async for event in engine.research(topic, enabled_sites):
            yield event  # SSE research_progress events
    """

    def __init__(
        self,
        citation_manager: CitationManager,
        research_context: ResearchContext,
        max_steps: int | None = None,
    ):
        self.cm = citation_manager
        self.rc = research_context
        self.max_steps = max_steps or AGENT_MAX_STEPS
        self._fetched_urls: set[str] = set()
        self._total_chunks_stored = 0

    async def research(
        self,
        topic: str,
        enabled_sites: list[str] | None = None,
    ) -> AsyncIterator[dict]:
        """Run the research phase (ReAct loop), yielding SSE events.

        For every source found:
          - Metadata → self.cm.add(...)   (via agent_tools)
          - Full text → self.rc.add(...)  (post-processed here)

        Args:
            topic: Research topic.
            enabled_sites: List of site IDs to prefer. Empty = Tavily only.

        Yields:
            SSE event dicts: research_progress, research_source_found, research_done.
        """
        enabled_sites = enabled_sites or []
        client = get_client()
        model = get_model()

        # Build initial user message with site guidance
        user_message = topic
        if enabled_sites:
            site_list = ", ".join(enabled_sites)
            user_message = (
                f"研究主题: {topic}\n\n"
                f"请优先从以下权威站点获取信息: {site_list}。"
                f"使用 search_site 工具分别搜索这些站点，"
                f"同时用 search_web 补充通用搜索。"
            )
        else:
            user_message = (
                f"研究主题: {topic}\n\n"
                f"请使用 search_web 从多个角度搜索相关信息，"
                f"对重要结果用 fetch_url 获取全文。"
            )

        messages: list[dict] = [
            {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        step_num = 0
        hit_max_steps = False
        tools_called_count = 0  # Track total tool invocations across all steps
        research_start = time.monotonic()

        while step_num < self.max_steps:
            step_num += 1
            step_start = time.monotonic()

            # --- LLM Call ---
            try:
                stream = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    temperature=0.3,
                    stream=True,
                )
            except Exception as e:
                logger.error("LLM call failed at step %d: %s", step_num, e)
                yield _sse("research_progress", {
                    "ts": _now(),
                    "icon": "❌",
                    "message": f"LLM调用失败: {e}",
                })
                break

            # --- Stream & Accumulate ---
            collected_content = ""
            collected_tool_calls: dict[int, dict] = {}

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in collected_tool_calls:
                            collected_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_delta.id:
                            collected_tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                collected_tool_calls[idx]["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                collected_tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments

                if delta.content:
                    collected_content += delta.content

            # --- Check if done (no tool calls = Phase 1 complete) ---
            # Require at least 2 rounds of tool calls before stopping,
            # to prevent lazy LLM from quitting after one search.
            MIN_TOOL_ROUNDS = 2
            if not collected_tool_calls and tools_called_count >= MIN_TOOL_ROUNDS:
                logger.info("Research phase complete after %d steps, %d tools",
                            step_num, tools_called_count)
                break
            elif not collected_tool_calls:
                logger.info("LLM wanted to stop early (only %d tools so far), "
                            "pushing for more research", tools_called_count)
                # Prompt the LLM to continue searching
                messages.append({
                    "role": "user",
                    "content": (
                        "你收集的信息还不足够。请至少再从 2 个不同角度进行搜索，"
                        "或对已有的关键结果用 fetch_url 获取完整正文。"
                        "不要急于结束研究。"
                    ),
                })
                continue

            # --- Yield thought as progress ---
            if collected_content.strip():
                yield _sse("research_progress", {
                    "ts": _now(),
                    "icon": "💭",
                    "message": collected_content.strip()[:300],
                })

            # --- Build tool_calls list ---
            tool_calls_list = [
                {
                    "id": collected_tool_calls[i]["id"],
                    "type": "function",
                    "function": collected_tool_calls[i]["function"],
                }
                for i in sorted(collected_tool_calls.keys())
            ]

            # --- Yield progress: how many tools ---
            n_tools = len(tool_calls_list)
            yield _sse("research_progress", {
                "ts": _now(),
                "icon": "🔄",
                "message": f"并行执行 {n_tools} 个工具调用",
            })

            # --- Append assistant message ---
            messages.append({
                "role": "assistant",
                "content": collected_content or None,
                "tool_calls": tool_calls_list,
            })

            # --- Execute tools (parallel, with URL dedup) ---
            tool_tasks = []
            for tc in tool_calls_list:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {"error": "参数解析失败"}

                # URL dedup for fetch_url
                is_dup = False
                if fn_name == "fetch_url":
                    url = fn_args.get("url", "")
                    if url in self._fetched_urls:
                        is_dup = True
                    else:
                        self._fetched_urls.add(url)

                tool_tasks.append((tc, fn_name, fn_args, is_dup))

            # Parallel execution with per-tool timeout
            async def _exec_with_timeout(fn_name, fn_args, is_dup):
                if is_dup:
                    return "[跳过] URL已获取过"
                try:
                    return await asyncio.wait_for(
                        execute_tool(fn_name, fn_args, citation_manager=self.cm),
                        timeout=PER_SEARCH_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Tool %s timed out after %ds", fn_name, PER_SEARCH_TIMEOUT)
                    return f"[超时] {fn_name} 执行超过 {PER_SEARCH_TIMEOUT}s，已跳过"

            parallel_start = time.monotonic()
            results = await asyncio.gather(
                *[_exec_with_timeout(fn_name, fn_args, is_dup)
                  for _, fn_name, fn_args, is_dup in tool_tasks],
                return_exceptions=True,
            )
            parallel_elapsed = time.monotonic() - parallel_start

            # Increment tool invocation counter
            tools_called_count += len([1 for _, _, _, is_dup in tool_tasks if not is_dup])

            # --- Yield progress for each action ---
            for (tc, fn_name, fn_args, is_dup), observation in zip(tool_tasks, results):
                if isinstance(observation, Exception):
                    observation = f"工具执行异常: {str(observation)}"

                # Progress event
                icon_map = {
                    "search_web": "🔍",
                    "search_site": "📚",
                    "fetch_url": "📄",
                }
                icon = icon_map.get(fn_name, "🔧")
                desc = _tool_description(fn_name, fn_args)
                yield _sse("research_progress", {
                    "ts": _now(),
                    "icon": icon,
                    "message": f"{desc}" + (" (跳过-重复)" if is_dup else ""),
                })

                # Add tool result to conversation
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(observation)[:2000],  # Truncate for LLM context
                })

                # --- ★ KEY: Capture full text into ResearchContext ★ ---
                await self._capture_full_text(fn_name, fn_args, observation, is_dup)

            # --- Yield step summary ---
            yield _sse("research_progress", {
                "ts": _now(),
                "icon": "✅",
                "message": (
                    f"步骤 {step_num} 完成 ({parallel_elapsed:.1f}s)，"
                    f"已收集 {self.cm.count} 个来源"
                ),
            })

            step_elapsed = time.monotonic() - step_start
            logger.info(
                "Research step %d/%d done in %.1fs, %d sources so far",
                step_num, self.max_steps, step_elapsed, self.cm.count,
            )

        else:
            # Max steps reached
            hit_max_steps = True
            yield _sse("research_progress", {
                "ts": _now(),
                "icon": "⚠️",
                "message": f"研究已达最大步数 ({self.max_steps})，进入大纲生成阶段",
            })

        # --- Yield research_done ---
        elapsed = time.monotonic() - research_start
        yield {
            "event": "research_done",
            "data": json.dumps({
                "sources": self.cm.count,
                "elapsed_s": round(elapsed, 1),
                "chunks_stored": self._total_chunks_stored,
                "hit_max_steps": hit_max_steps,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Full-text capture for ResearchContext
    # ------------------------------------------------------------------

    async def _capture_full_text(
        self,
        fn_name: str,
        fn_args: dict,
        observation: Any,
        is_dup: bool,
    ) -> None:
        """Post-process a tool result: fetch full text of discovered sources → RC.

        For search_web / search_site: extracts URLs from results, fetches each.
        For fetch_url: the observation already contains the article text.
        """
        if is_dup:
            return

        observation_str = str(observation) if not isinstance(observation, str) else observation

        if fn_name in ("search_web", "search_site"):
            # Extract URLs from the formatted observation and fetch each
            urls = _extract_urls(observation_str)
            for url in urls:
                if url in self._fetched_urls:
                    continue
                self._fetched_urls.add(url)

                try:
                    result = await asyncio.wait_for(
                        fetch_url_content(url, max_chars=8000),
                        timeout=PER_SEARCH_TIMEOUT,
                    )
                    if result.get("content") and not result.get("error"):
                        title = _extract_title_from_content(result["content"]) or url
                        site = _guess_site_name(url)
                        n = await self.rc.add(
                            content=result["content"],
                            url=url,
                            site=site,
                            title=title,
                        )
                        self._total_chunks_stored += n
                        logger.debug(
                            "RC: stored %d chunks from %s (%s)", n, title[:60], url,
                        )
                except asyncio.TimeoutError:
                    logger.warning("Full-text fetch timed out for %s", url)
                except Exception as e:
                    logger.warning("Full-text fetch failed for %s: %s", url, e)

        elif fn_name == "fetch_url":
            # The observation already contains the fetched content
            url = fn_args.get("url", "")
            if not url:
                return

            # Re-fetch with larger max_chars for full text (observation was truncated)
            try:
                result = await asyncio.wait_for(
                    fetch_url_content(url, max_chars=8000),
                    timeout=PER_SEARCH_TIMEOUT,
                )
                if result.get("content") and not result.get("error"):
                    title = _extract_title_from_content(result["content"]) or url
                    site = _guess_site_name(url)
                    n = await self.rc.add(
                        content=result["content"],
                        url=url,
                        site=site,
                        title=title,
                    )
                    self._total_chunks_stored += n
                    logger.debug(
                        "RC: stored %d chunks from fetch_url %s", n, url,
                    )
            except asyncio.TimeoutError:
                logger.warning("Full-text fetch timed out for %s", url)
            except Exception as e:
                logger.warning("Full-text fetch failed for %s: %s", url, e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO timestamp for progress events."""
    return datetime.now(timezone.utc).isoformat()


def _sse(event: str, data: dict) -> dict:
    """Build an SSE event dict."""
    return {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False),
    }


def _tool_description(fn_name: str, fn_args: dict) -> str:
    """Human-readable description of a tool call."""
    if fn_name == "search_web":
        return f"搜索网页: {fn_args.get('query', '')[:80]}"
    elif fn_name == "search_site":
        return f"搜索 {fn_args.get('site_id', '')}: {fn_args.get('query', '')[:80]}"
    elif fn_name == "fetch_url":
        return f"抓取网页: {fn_args.get('url', '')[:80]}"
    else:
        return f"{fn_name}"


def _extract_urls(text: str) -> list[str]:
    """Extract URLs from a formatted tool observation."""
    import re
    # Match "URL: https://..." pattern from tool output
    urls = re.findall(r'URL:\s*(https?://[^\s\n]+)', text)
    # Dedup while preserving order
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _extract_title_from_content(content: str) -> str:
    """Extract a title from the first line of fetched content."""
    if not content:
        return ""
    first_line = content.strip().split("\n")[0]
    # Strip markdown heading markers
    first_line = first_line.lstrip("#").strip()
    # Jina prepends "Title: " sometimes
    if first_line.lower().startswith("title:"):
        first_line = first_line[6:].strip()
    return first_line[:200]


def _guess_site_name(url: str) -> str:
    """Guess a site name from a URL."""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    domain_map = {
        "pubmed.ncbi.nlm.nih.gov": "PubMed",
        "arxiv.org": "arXiv",
        "semanticscholar.org": "Semantic Scholar",
        "who.int": "WHO",
        "cdc.gov": "CDC",
        "github.com": "GitHub",
        "wikipedia.org": "Wikipedia",
        "news.google.com": "Google News",
    }
    for key, name in domain_map.items():
        if key in domain:
            return name
    # Return domain without TLD
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain
