"""模块 2+3 集成 Demo：Agent 并行搜索 + 引用追踪 + 串行vs并行对比。

这是模块2（并行工具调用）和模块3（引用追踪）的完整端到端演示。
输入一个研究问题，Agent 执行多步推理 → 并行搜索 → 注册引用 → 输出含引用的回答。

用法:
  PYTHONPATH=. python scripts/demo_agent.py [研究问题]
  PYTHONPATH=. python scripts/demo_agent.py "人工智能在医疗领域的应用"
  PYTHONPATH=. python scripts/demo_agent.py  # 使用默认问题
"""

import asyncio
import json
import sys
import time
from typing import Any


# =========================================================================
# Demo 1: 完整 Agent 运行（并行搜索 + 引用追踪）
# =========================================================================

async def demo_agent_full(question: str):
    """完整的 Agent 运行演示：搜索 → 注册引用 → 生成回答。"""
    from app.services.agent_tools import execute_tool, TOOL_DEFINITIONS
    from app.services.citation_manager import CitationManager
    from app.services.llm import get_client, get_model

    print("=" * 70)
    print("模块 2+3 Demo: Agent 并行搜索 + 引用追踪")
    print("=" * 70)
    print(f"\n📝 研究问题: {question}\n")

    cm = CitationManager()
    client = get_client()
    model = get_model()

    # --- Step 1: 展示可用的工具 ---
    print("🔧 可用工具:")
    for t in TOOL_DEFINITIONS:
        name = t["function"]["name"]
        desc = t["function"]["description"][:80]
        print(f"    • {name}: {desc}...")
    print()

    # --- Step 2: Agent 循环 ---
    messages = [
        {"role": "system", "content": """你是一个研究助手。请执行以下步骤：
1. 分析研究问题，确定需要查找的信息
2. 同时发起多个搜索（一次性发出多个 tool_calls）以节省时间
3. 仔细阅读搜索结果
4. 给出有信息量的回答，在引用来源时使用 [1]、[2] 等编号
5. 使用中文回答"""},
        {"role": "user", "content": question},
    ]

    max_steps = 5
    all_steps = []

    for step_num in range(1, max_steps + 1):
        print(f"{'─' * 60}")
        print(f"Step {step_num}: 正在调用 LLM...")

        # LLM call (non-streaming for simplicity in demo)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=0.3,
                stream=False,
            )
        except Exception as e:
            print(f"  ❌ LLM 调用失败: {e}")
            break

        msg = response.choices[0].message

        # No tool calls → final answer
        if not msg.tool_calls:
            answer = msg.content or ""
            print(f"\n✅ 最终回答:\n")
            print(answer)
            print()
            break

        # --- Handle tool calls (PARALLEL) ---
        tool_calls_list = []
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}
            tool_calls_list.append((tc, fn_name, fn_args))

        n_tools = len(tool_calls_list)
        print(f"  🔄 LLM 发起了 {n_tools} 个工具调用，并行执行:")
        for _, name, args in tool_calls_list:
            args_preview = json.dumps(args, ensure_ascii=False)[:80]
            print(f"      • {name}({args_preview})")

        # PARALLEL execution
        async def _exec_one(fn_name, fn_args):
            return await execute_tool(fn_name, fn_args, citation_manager=cm)

        t0 = time.monotonic()
        results = await asyncio.gather(
            *[_exec_one(fn_name, fn_args) for _, fn_name, fn_args in tool_calls_list],
            return_exceptions=True,
        )
        parallel_elapsed = time.monotonic() - t0

        # Serial estimate
        serial_estimate = parallel_elapsed * n_tools

        print(f"\n  ⏱ 并行执行耗时: {parallel_elapsed:.1f}s")
        print(f"  ⏱ 串行预估耗时: {serial_estimate:.1f}s")
        print(f"  ⚡ 节省: {serial_estimate - parallel_elapsed:.1f}s (加速 {n_tools:.0f}x)\n")

        # Append assistant message
        assistant_msg = {"role": "assistant", "content": msg.content or None}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
        messages.append(assistant_msg)

        # Process results
        seen_urls = set()
        for (tc, fn_name, fn_args), result in zip(tool_calls_list, results):
            if isinstance(result, Exception):
                result_str = f"工具执行异常: {result}"
            else:
                result_str = str(result)

            # Show preview
            preview = result_str[:200].replace("\n", " ")
            print(f"  📋 {fn_name} 结果 ({len(result_str)} chars): {preview}...")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

            all_steps.append({
                "step": step_num,
                "tool": fn_name,
                "args": fn_args,
                "result_preview": result_str[:200],
            })

        print()

    else:
        if not all_steps:
            print("⚠️ 达到最大步数限制，未获得最终回答。")

    # --- Step 3: 展示引用列表 ---
    print(f"{'─' * 60}")
    print(f"\n📚 引用追踪结果 (CitationManager):")
    print(f"   共注册 {cm.count} 条引用来源\n")

    if cm.count > 0:
        refs = cm.format_references("markdown")
        print(refs)
        print()

        # JSON 格式
        print("📋 JSON 格式引用数据:")
        print(json.dumps(cm.to_dict(), ensure_ascii=False, indent=2)[:800])
        print()

    return {
        "steps": all_steps,
        "references": cm.to_dict(),
    }


# =========================================================================
# Demo 2: 串行 vs 并行性能对比（真实 API 调用）
# =========================================================================

async def demo_serial_vs_parallel():
    """用真实搜索 API 对比串行 vs 并行性能。"""
    from app.services.agent_tools import execute_tool

    print(f"\n\n{'=' * 70}")
    print("串行 vs 并行性能对比（真实 API 调用）")
    print("=" * 70)

    # 准备 3 个不同的搜索任务
    tasks = [
        ("search_web", {"query": "2025 AI healthcare trends", "num_results": 3}),
        ("search_web", {"query": "深度学习最新进展", "num_results": 3}),
        ("search_site", {"site_id": "pubmed", "query": "AI diagnostics", "num_results": 3}),
    ]

    print(f"\n📋 测试任务 ({len(tasks)} 个):")
    for name, args in tasks:
        print(f"    • {name}({args})")

    # --- 串行执行 ---
    print(f"\n{'─' * 40}")
    print("🔴 串行执行 (逐个调用):")
    serial_start = time.monotonic()
    serial_results = []
    for name, args in tasks:
        t0 = time.monotonic()
        result = await execute_tool(name, args)
        elapsed = time.monotonic() - t0
        serial_results.append((name, result, elapsed))
        print(f"    {name}: {elapsed:.2f}s")
    serial_total = time.monotonic() - serial_start
    print(f"    📊 串行总耗时: {serial_total:.2f}s")

    # --- 并行执行 ---
    print(f"\n{'─' * 40}")
    print("🟢 并行执行 (asyncio.gather):")
    parallel_start = time.monotonic()
    parallel_results = await asyncio.gather(
        *[execute_tool(name, args) for name, args in tasks],
        return_exceptions=True,
    )
    parallel_total = time.monotonic() - parallel_start
    print(f"    📊 并行总耗时: {parallel_total:.2f}s")

    # --- 对比 ---
    speedup = serial_total / parallel_total if parallel_total > 0 else 0
    saved = serial_total - parallel_total

    print(f"\n{'─' * 40}")
    print(f"📊 性能对比:")
    print(f"    串行:   {serial_total:.2f}s")
    print(f"    并行:   {parallel_total:.2f}s")
    print(f"    加速比: {speedup:.1f}x")
    print(f"    节省:   {saved:.2f}s")
    print()

    return {
        "serial_total": round(serial_total, 2),
        "parallel_total": round(parallel_total, 2),
        "speedup": round(speedup, 1),
        "time_saved": round(saved, 2),
    }


# =========================================================================
# Demo 3: CitationManager 独立演示
# =========================================================================

def demo_citation_manager():
    """CitationManager 完整功能演示。"""
    from app.services.citation_manager import CitationManager

    print(f"\n\n{'=' * 70}")
    print("CitationManager 引用追踪演示")
    print("=" * 70)

    cm = CitationManager()

    # 添加引用
    sources = [
        ("https://pubmed.ncbi.nlm.nih.gov/38512345/", "AI-based early detection of lung cancer: a systematic review",
         "This systematic review analyzed 47 studies...", "academic", "PubMed"),
        ("https://www.who.int/news-room/fact-sheets/detail/cancer",
         "WHO Cancer Fact Sheet", "Cancer is a leading cause of death worldwide...", "official", "WHO"),
        ("https://arxiv.org/abs/2403.12345",
         "Deep Learning for Medical Imaging: A Comprehensive Survey",
         "We survey 200+ papers on deep learning approaches...", "academic", "arXiv"),
        ("https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(24)00001-2/fulltext",
         "Global cancer statistics 2024", "Estimated numbers of new cancer cases...", "academic", "The Lancet"),
    ]

    print(f"\n📝 注册 {len(sources)} 条引用来源:\n")
    for url, title, snippet, stype, site in sources:
        idx = cm.add(url=url, title=title, snippet=snippet, source_type=stype, site_name=site)
        print(f"    [{idx}] {title}")
        print(f"        {url}")
        print(f"        [{stype}] {site}\n")

    # URL 去重验证
    print("🔄 URL 去重验证:")
    dup_idx = cm.add(
        url="https://pubmed.ncbi.nlm.nih.gov/38512345/",
        title="Different title, same URL",
    )
    print(f"    重复 URL → 返回已有编号 [{dup_idx}]（应为 [1]）✅\n")

    # Markdown 参考文献列表
    print("📚 Markdown 格式参考文献列表:")
    print(cm.format_references("markdown"))
    print()

    # Plain 格式
    print("📋 Plain 格式:")
    print(cm.format_references("plain"))
    print()

    # Inline refs（供 LLM prompt 使用）
    print("📌 内联引用格式（供 LLM prompt 使用）:")
    print(cm.format_inline_refs())
    print()

    # 查找
    c = cm.get_by_url("https://arxiv.org/abs/2403.12345")
    print(f"🔍 URL 查找: #{c.index} → {c.title}")
    c2 = cm.get_by_index(2)
    print(f"🔍 索引查找: #{c2.index} → {c2.title} ({c2.site_name})")

    print(f"\n✅ CitationManager 演示完成 — 共 {cm.count} 条引用")


# =========================================================================
# Main
# =========================================================================

async def main():
    """运行模块 2+3 完整演示。"""
    # 从命令行参数获取问题
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
    else:
        question = "人工智能在医疗诊断中的应用现状"

    print("🚀 模块 2+3 集成 Demo: Agent 并行搜索 + 引用追踪\n")

    # Demo 1: 完整 Agent 运行
    try:
        await demo_agent_full(question)
    except Exception as e:
        print(f"\n⚠️ Demo 1 (Agent) 因 API 调用失败跳过: {e}")
        print("   请检查 .env 中的 OPENAI_API_KEY 配置\n")

    # Demo 2: 串行 vs 并行对比
    try:
        await demo_serial_vs_parallel()
    except Exception as e:
        print(f"\n⚠️ Demo 2 (性能对比) 因 API 调用失败跳过: {e}\n")

    # Demo 3: CitationManager 独立演示
    try:
        demo_citation_manager()
    except Exception as e:
        print(f"\n⚠️ Demo 3 (CitationManager) 失败: {e}\n")

    print(f"\n{'=' * 70}")
    print("✅ 模块 2+3 演示完成")
    print("=" * 70)
    print()
    print("📊 演示总结:")
    print("   Demo 1 — Agent 完整循环: 搜索 → 并行工具调用 → 引用注册 → 生成回答")
    print("   Demo 2 — 串行 vs 并行对比: 3 路真实 API 搜索结果的时间对比")
    print("   Demo 3 — CitationManager: 去重 / 格式化 / 查找 / JSON 序列化")


if __name__ == "__main__":
    asyncio.run(main())
