"""ReAct Agent Engine — Week 6 升级版.

Key improvements over Week 5:
  1. Parallel tool execution — uses asyncio.gather for concurrent tool_calls
  2. Citation tracking — all search/fetch results registered in CitationManager
  3. Real search tools — Tavily search_web, Jina fetch_url, site search

Streaming SSE events:
  - thought: Agent reasoning text
  - action: Tool name + args (yielded before execution)
  - observation: Tool result
  - answer_chunk: Token-by-token answer streaming
  - answer: Final complete answer
  - citations: Reference list
  - done: Session summary
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
from app.utils.config import AGENT_MAX_STEPS

logger = logging.getLogger(__name__)

# System prompt for the research agent
SYSTEM_PROMPT = """你是一个AI研究助手，为研究报告撰写提供信息支持。

你的能力:
- search_web: 搜索互联网获取最新信息
- fetch_url: 获取网页正文进行深度阅读
- search_site: 在权威站点（PubMed/arXiv/WHO/CDC等）内搜索，获取高质量学术/官方信息
- calculator: 进行数值计算
- get_current_time: 获取当前时间

工作流程:
1. 收到研究主题后，分析需要哪些方面的信息
2. **同时发起多个搜索/抓取** 以节省时间。一次性发起多个 tool_calls（如同时搜索 PubMed + WHO + arXiv），系统会并行执行
3. 仔细阅读搜索返回的结果
4. 如果需要深入了解，用 fetch_url 读取完整文章
5. 给出详细的、有信息量的回答

重要规则:
- 基于工具返回的实际结果回答，不要编造信息
- 在回答中引用来源时，使用生成好的引用编号 [1]、[2] 等
- 优先从权威来源（PubMed、WHO等）获取信息
- **第一步就同时发起多个搜索**，例如同时搜 PubMed、WHO、通用搜索引擎
- **不要重复抓取同一个 URL**，如果之前已经获取过某 URL 的全文，直接用那个结果
- 如果一步完成了所有搜索任务，下一步直接给出回答，不要再继续调用工具
- 回复使用中文，专业术语可保留英文"""


async def run_agent_stream(
    question: str,
    session_id: str,
    max_steps: int | None = None,
) -> AsyncIterator[dict]:
    """Run ReAct agent with parallel tool calls and citation tracking.

    Args:
        question: Research question.
        session_id: Unique session identifier.
        max_steps: Max reasoning steps (default from config).

    Yields:
        SSE event dicts: {event: str, data: str}.
    """
    max_steps = max_steps or AGENT_MAX_STEPS
    client = get_client()
    model = get_model()

    # Initialize citation tracking for this run
    cm = CitationManager()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    steps: list[dict] = []
    step_num = 0
    final_answer = ""
    hit_max_steps = False
    fetched_urls: set[str] = set()  # Track fetched URLs to prevent duplicates

    # Performance tracking
    serial_time_total = 0.0    # Simulated: what if tools ran serially?
    parallel_time_total = 0.0  # Actual: with asyncio.gather

    while step_num < max_steps:
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
            yield {
                "event": "thought",
                "data": json.dumps(
                    {"step": step_num, "thought": f"LLM调用失败: {str(e)}"},
                    ensure_ascii=False,
                ),
            }
            final_answer = "抱歉，AI服务暂时不可用，请稍后重试。"
            break

        # --- Stream & Accumulate ---
        collected_content = ""
        collected_tool_calls: dict[int, dict] = {}
        answer_chunks_yielded = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Accumulate tool calls
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

            # Stream content tokens as answer_chunk
            if delta.content:
                collected_content += delta.content
                if not collected_tool_calls:
                    answer_chunks_yielded = True
                    yield {
                        "event": "answer_chunk",
                        "data": json.dumps({"chunk": delta.content}, ensure_ascii=False),
                    }

        # --- Handle Tool Calls (PARALLEL execution) ---
        if collected_tool_calls:
            # Reset answer chunks if any were sent
            if answer_chunks_yielded:
                yield {
                    "event": "answer_reset",
                    "data": json.dumps({"reason": "tool_call"}, ensure_ascii=False),
                }

            # Yield thought
            if collected_content.strip():
                yield {
                    "event": "thought",
                    "data": json.dumps(
                        {"step": step_num, "thought": collected_content.strip()},
                        ensure_ascii=False,
                    ),
                }

            # Build tool_calls list
            tool_calls_list = [
                {
                    "id": collected_tool_calls[i]["id"],
                    "type": "function",
                    "function": collected_tool_calls[i]["function"],
                }
                for i in sorted(collected_tool_calls.keys())
            ]

            # Notify: how many tools, serial vs parallel
            n_tools = len(tool_calls_list)
            if n_tools > 1:
                yield {
                    "event": "thought",
                    "data": json.dumps(
                        {"step": step_num,
                         "thought": f"🔄 并行执行 {n_tools} 个工具调用（比串行快约 {n_tools}x）"},
                        ensure_ascii=False,
                    ),
                }

            # Append assistant message with tool_calls
            messages.append({
                "role": "assistant",
                "content": collected_content or None,
                "tool_calls": tool_calls_list,
            })

            # --- PARALLEL EXECUTION (the key change from Week 5) ---
            # Build coroutines with URL dedup for fetch_url
            tool_coros = []
            for tc in tool_calls_list:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {"error": "参数解析失败"}

                # URL dedup: skip duplicate fetch_url calls
                if fn_name == "fetch_url":
                    url = fn_args.get("url", "")
                    if url in fetched_urls:
                        tool_coros.append((tc, fn_name, fn_args, True, url))
                    else:
                        fetched_urls.add(url)
                        tool_coros.append((tc, fn_name, fn_args, False, url))
                else:
                    tool_coros.append((tc, fn_name, fn_args, False, None))

            # Notify actions (before execution)
            for tc, fn_name, fn_args, is_dup, dedup_url in tool_coros:
                yield {
                    "event": "action",
                    "data": json.dumps(
                        {"step": step_num, "tool": fn_name, "input": fn_args,
                         "skipped": is_dup},
                        ensure_ascii=False,
                    ),
                }

            # Measure parallel execution
            # Add _execute_or_skip helper inline
            async def _exec(fn_name, fn_args, is_dup, url=""):
                if is_dup:
                    return f"[跳过重复抓取] URL '{url}' 已在之前获取过，跳过以减少 API 调用。"
                return await execute_tool(fn_name, fn_args, citation_manager=cm)

            parallel_start = time.monotonic()

            results = await asyncio.gather(
                *[_exec(fn_name, fn_args, is_dup, url or "")
                  for _, fn_name, fn_args, is_dup, url in tool_coros],
                return_exceptions=True,
            )

            parallel_elapsed = time.monotonic() - parallel_start
            parallel_time_total += parallel_elapsed

            # Calculate simulated serial time
            serial_time_total += parallel_elapsed * n_tools

            # Yield observations
            for i, ((tc, fn_name, fn_args, is_dup, url), observation) in enumerate(zip(tool_coros, results)):
                if isinstance(observation, Exception):
                    observation = f"工具执行异常: {str(observation)}"

                display = observation[:500] + "..." if len(observation) > 500 else observation

                yield {
                    "event": "observation",
                    "data": json.dumps(
                        {"step": step_num, "tool": fn_name, "result": display},
                        ensure_ascii=False,
                    ),
                }

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(observation),
                })

                steps.append({
                    "step": step_num,
                    "thought": collected_content.strip(),
                    "action": fn_name,
                    "input": fn_args,
                    "observation": str(observation)[:500],
                })

            step_elapsed = time.monotonic() - step_start

            yield {
                "event": "thought",
                "data": json.dumps(
                    {"step": step_num,
                     "thought": (
                         f"✅ {n_tools} 个工具并行执行完成 ({parallel_elapsed:.1f}s)。"
                         f"串行预估: {parallel_elapsed * n_tools:.1f}s，节省约 "
                         f"{(parallel_elapsed * n_tools - parallel_elapsed):.1f}s"
                     )},
                    ensure_ascii=False,
                ),
            }

        else:
            # No tool calls — final answer
            final_answer = collected_content
            break

    else:
        # Max steps reached
        hit_max_steps = True
        if not final_answer:
            final_answer = "抱歉，在限定步骤内无法完成研究。请尝试缩小研究范围。"

    # --- Yield final answer ---
    yield {
        "event": "answer",
        "data": json.dumps(
            {
                "answer": final_answer,
                "hit_max_steps": hit_max_steps,
                "total_steps": len(steps),
                "citations_count": cm.count,
            },
            ensure_ascii=False,
        ),
    }

    # --- Yield citations ---
    if cm.count > 0:
        yield {
            "event": "citations",
            "data": json.dumps(
                {
                    "count": cm.count,
                    "references": cm.format_references(),
                    "citations_json": cm.to_dict(),
                },
                ensure_ascii=False,
            ),
        }

    # --- Yield performance stats ---
    yield {
        "event": "perf",
        "data": json.dumps(
            {
                "parallel_total_s": round(parallel_time_total, 2),
                "serial_estimate_s": round(serial_time_total, 2),
                "time_saved_s": round(serial_time_total - parallel_time_total, 2),
                "speedup": (
                    round(serial_time_total / parallel_time_total, 1)
                    if parallel_time_total > 0 else 1.0
                ),
            },
            ensure_ascii=False,
        ),
    }

    # --- Done ---
    yield {
        "event": "done",
        "data": json.dumps(
            {
                "session_id": session_id,
                "total_steps": len(steps),
                "citations": cm.count,
                "hit_max_steps": hit_max_steps,
            },
            ensure_ascii=False,
        ),
    }
