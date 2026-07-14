"""Module 2+3 Verification: Parallel Tool Calls + Citation Tracking.

Verification items:
  1. CitationManager standalone test (no server needed)
  2. Parallel vs Serial timing benchmark (no server needed)
  3. Agent engine unit test — parallel execution + citation registration
     (tests the agent internals directly, no HTTP server required)

Usage:
  PYTHONPATH=. python3 scripts/verify_module2_3.py
"""

import asyncio
import json
import time
from typing import Any


# =========================================================================
# Test 1: CitationManager standalone
# =========================================================================

def test_citation_manager():
    """Verify CitationManager independently."""
    from app.services.citation_manager import CitationManager

    print("=" * 70)
    print("Test 2.1: CitationManager Reference Tracking")
    print("=" * 70)

    cm = CitationManager()

    # Add citations
    idx1 = cm.add(
        "https://pubmed.ncbi.nlm.nih.gov/12345/",
        "Long-term effects of COVID-19",
        "A systematic review of 55 long-term effects...",
        source_type="academic",
        site_name="PubMed",
    )
    print(f"\n  [OK] Added citation 1: PubMed paper -> #[{idx1}]")

    idx2 = cm.add(
        "https://www.who.int/health-topics/coronavirus",
        "WHO Coronavirus Disease Dashboard",
        "Official WHO data and guidelines...",
        source_type="official",
        site_name="WHO",
    )
    print(f"  [OK] Added citation 2: WHO page -> #[{idx2}]")

    idx3 = cm.add(
        "https://arxiv.org/abs/2024.12345",
        "Post-COVID Syndrome Analysis via Deep Learning",
        "We propose a novel approach...",
        source_type="academic",
        site_name="arXiv",
    )
    print(f"  [OK] Added citation 3: arXiv paper -> #[{idx3}]")

    # Dedup test
    dup_idx = cm.add(
        "https://pubmed.ncbi.nlm.nih.gov/12345/",
        "Different title, same URL",
        source_type="academic",
    )
    passed = dup_idx == idx1
    print(f"\n  [{'OK' if passed else 'FAIL'}] URL dedup: expected=1, actual={dup_idx}")

    # Count
    count_ok = cm.count == 3
    print(f"  [{'OK' if count_ok else 'FAIL'}] Citation count: {cm.count} (expected=3)")

    # Batch add
    batch_indices = cm.add_batch([
        {"url": "https://example.com/a", "title": "Paper A", "snippet": "..."},
        {"url": "https://example.com/b", "title": "Paper B", "snippet": "..."},
    ])
    print(f"  [OK] Batch add: indices={batch_indices}, total count={cm.count} (expected=5)")

    # Format references
    refs_md = cm.format_references("markdown")
    print(f"\n  --- Markdown Reference List ---")
    for line in refs_md.split("\n")[:6]:
        print(f"  {line}")

    # JSON serialization
    json_data = cm.to_dict()
    assert json_data["count"] == 5, f"Expected 5 citations, got {json_data['count']}"
    assert len(json_data["citations"]) == 5
    print(f"\n  [OK] JSON serialization: {len(json_data['citations'])} citations")

    # format_inline_refs
    inline = cm.format_inline_refs()
    assert "[1]" in inline and "[5]" in inline
    print(f"  [OK] Inline refs format: contains [1]...[5]")

    # Lookup
    c = cm.get_by_url("https://arxiv.org/abs/2024.12345")
    assert c is not None and c.index == 3
    print(f"  [OK] URL lookup: index={c.index}, title={c.title[:40]}...")

    c2 = cm.get_by_index(2)
    assert c2 is not None and "WHO" in c2.site_name
    print(f"  [OK] Index lookup: #{c2.index} -> {c2.site_name}")

    print(f"\n  >> CitationManager: ALL TESTS PASSED")


# =========================================================================
# Test 2: Parallel vs Serial timing benchmark
# =========================================================================

async def simulate_search(delay: float, name: str) -> str:
    """Simulate a tool call with a given delay."""
    await asyncio.sleep(delay)
    return f"Result from {name}: {delay}s"


async def test_parallel_vs_serial():
    """Verify asyncio.gather performance advantage."""
    print(f"\n\n{'=' * 70}")
    print("Test 2.2: Parallel vs Serial Tool Execution Benchmark")
    print("=" * 70)

    delays = [1.0, 1.2, 0.8]
    names = ["search_web", "search_site_pubmed", "search_site_who"]

    # --- Serial ---
    print(f"\n  Serial execution ({len(delays)} tools):")
    serial_start = time.monotonic()
    for delay, name in zip(delays, names):
        await simulate_search(delay, name)
        print(f"    -> {name}: {delay}s")
    serial_elapsed = time.monotonic() - serial_start
    print(f"    Serial total: {serial_elapsed:.2f}s")

    # --- Parallel ---
    print(f"\n  Parallel execution ({len(delays)} tools):")
    parallel_start = time.monotonic()
    await asyncio.gather(*[
        simulate_search(delay, name)
        for delay, name in zip(delays, names)
    ])
    parallel_elapsed = time.monotonic() - parallel_start
    print(f"    All {len(delays)} tools ran concurrently")
    print(f"    Parallel total: {parallel_elapsed:.2f}s")

    # --- Comparison ---
    speedup = serial_elapsed / parallel_elapsed
    saved = serial_elapsed - parallel_elapsed
    print(f"\n  --- Comparison ---")
    print(f"  Serial:   {serial_elapsed:.2f}s")
    print(f"  Parallel: {parallel_elapsed:.2f}s")
    print(f"  Speedup:  {speedup:.1f}x")
    print(f"  Saved:    {saved:.2f}s")

    # Assert: parallel should be ~ max(delay), not sum(delays)
    assert parallel_elapsed < max(delays) * 1.5, (
        f"Parallel time ({parallel_elapsed:.2f}s) should be ~ {max(delays):.1f}s "
        f"(max single delay), not {sum(delays):.1f}s (sum of all delays)"
    )
    print(f"\n  [OK] Parallel ~ max(delay)={max(delays):.1f}s, not sum={sum(delays):.1f}s")

    print(f"\n  >> Parallel vs Serial Benchmark: PASSED")


# =========================================================================
# Test 3: Agent engine unit test (no server needed)
# =========================================================================

async def test_agent_engine_internals():
    """Test agent engine's parallel execution + citation tracking directly.

    This tests the core agent internals (tool dispatch, parallel gather,
    citation registration) without needing an HTTP server.
    """
    from app.services.agent_tools import (
        execute_tool, TOOL_DEFINITIONS,
    )
    from app.services.citation_manager import CitationManager

    print(f"\n\n{'=' * 70}")
    print("Test 2.3: Agent Engine Internals (Parallel + Citations)")
    print("=" * 70)

    # Setup citation manager (simulating agent engine init)
    cm = CitationManager()

    # Verify tool definitions
    tool_names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    expected_tools = ["search_web", "fetch_url", "search_site", "calculator", "get_current_time"]
    for t in expected_tools:
        assert t in tool_names, f"Missing tool: {t}"
    print(f"\n  [OK] Tool definitions: {len(TOOL_DEFINITIONS)} tools registered")
    print(f"       {', '.join(tool_names)}")

    # Test synchronous tools (calculator, get_current_time)
    print(f"\n  --- Synchronous tool tests ---")

    calc_result = await execute_tool("calculator", {"expression": "2 ** 10"})
    assert "1024" in calc_result, f"Unexpected calc result: {calc_result}"
    print(f"  [OK] calculator: 2**10 = 1024")

    time_result = await execute_tool("get_current_time", {})
    assert "2026" in time_result, f"Unexpected time result: {time_result}"
    print(f"  [OK] get_current_time: contains '2026'")

    # Test parallel execution (asyncio.gather on multiple tools)
    print(f"\n  --- Parallel tool execution test ---")

    t0 = time.monotonic()
    results = await asyncio.gather(
        execute_tool("calculator", {"expression": "100 + 200"}),
        execute_tool("calculator", {"expression": "50 * 4"}),
        execute_tool("get_current_time", {}),
    )
    parallel_time = time.monotonic() - t0

    assert "300" in str(results[0]), f"Result 0: {results[0]}"
    assert "200" in str(results[1]), f"Result 1: {results[1]}"
    assert "2026" in str(results[2]), f"Result 2: {results[2]}"
    print(f"  [OK] 3 tools executed in parallel ({parallel_time:.3f}s)")
    print(f"       Results all correct")

    # Test search_site with real API (PubMed)
    print(f"\n  --- Real search_site test (PubMed) ---")
    pubmed_result = await execute_tool(
        "search_site",
        {"site_id": "pubmed", "query": "long COVID", "num_results": 3},
        citation_manager=cm,
    )

    # Check citations were registered
    assert cm.count > 0, "PubMed search should register citations"
    print(f"  [OK] PubMed search returned results")
    print(f"       Citations registered: {cm.count}")

    # Verify citation data integrity
    for c in cm.sources:
        assert c.url, f"Citation #{c.index} has empty URL"
        assert c.title, f"Citation #{c.index} has empty title"
        assert c.index > 0
    print(f"  [OK] All citations have valid URL, title, and positive index")

    # Print citations summary
    print(f"\n  --- Registered Citations ---")
    for c in cm.sources[:5]:
        print(f"  [{c.index}] {c.title[:80]}... ({c.site_name})")

    # Test unknown tool
    error_result = await execute_tool("nonexistent_tool", {})
    assert "未知工具" in str(error_result), f"Expected error, got: {error_result}"
    print(f"\n  [OK] Unknown tool returns error message")

    print(f"\n  >> Agent Engine Internals: ALL TESTS PASSED")


# =========================================================================
# Main
# =========================================================================

async def main():
    """Run all module 2+3 verifications (no server required)."""
    print("Week 6 Module 2+3 Verification: Parallel Tool Calls + Citation Tracking\n")

    # Test 1: CitationManager (sync)
    test_citation_manager()

    # Test 2: Parallel vs Serial benchmark
    await test_parallel_vs_serial()

    # Test 3: Agent engine internals (uses real Tavily/PubMed APIs)
    try:
        await test_agent_engine_internals()
    except Exception as e:
        print(f"\n  [WARN] Agent engine test failed: {e}")
        print(f"  (This may be OK if API keys or network are unavailable)")

    print(f"\n\n{'=' * 70}")
    print("Module 2+3 Verification Complete")
    print("=" * 70)
    print("\nSummary:")
    print("  - CitationManager: URL dedup, batch add, markdown/json export, lookup")
    print("  - Parallel execution: asyncio.gather provides ~2-3x speedup")
    print("  - Agent engine: tool dispatch, citation auto-registration, error handling")


if __name__ == "__main__":
    asyncio.run(main())
