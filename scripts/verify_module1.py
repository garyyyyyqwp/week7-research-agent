"""模块 1 验证脚本：真实联网搜索 + 正文提取 + 定向站点抓取。

验证项目：
  1. Tavily 搜索：中文行业话题、英文学术话题、时事话题
  2. 正文提取：Jina Reader vs BeautifulSoup 对比
  3. 定向站点：PubMed + WHO 对比通用搜索

用法:
  python scripts/verify_module1.py
"""

import asyncio
import json
import time
from typing import Any


async def verify_tavily_search():
    """验证 1: 真实搜索 vs Mock 对比（3 个查询）。"""
    from app.services.web_search import search_web, search_web_mock

    queries = [
        ("中文行业话题", "新能源汽车2025年发展趋势"),
        ("英文学术话题", "transformer architecture attention mechanism latest research"),
        ("时事话题", "2026世界杯出线形势"),
    ]

    print("=" * 70)
    print("验证 1: 真实联网搜索 (Tavily)")
    print("=" * 70)

    for label, query in queries:
        print(f"\n{'─' * 60}")
        print(f"📝 [{label}] 查询: {query}")
        print(f"{'─' * 60}")

        # Tavily real search
        t0 = time.monotonic()
        real_results = await search_web(query, num_results=5)
        real_time = (time.monotonic() - t0) * 1000

        print(f"\n✅ Tavily 真实搜索 ({real_time:.0f}ms, {len(real_results)} 条结果):")
        for i, r in enumerate(real_results, 1):
            print(f"  [{i}] {r['title']}")
            print(f"      URL: {r['url']}")
            print(f"      摘要: {r['snippet'][:100]}...")
            if r.get("published_date"):
                print(f"      日期: {r['published_date']}")

        # Mock for comparison
        mock_results = await search_web_mock(query, num_results=5)

        print(f"\n📋 Mock 对比 ({len(mock_results)} 条结果):")
        for i, r in enumerate(mock_results, 1):
            print(f"  [{i}] {r['title']}")
            print(f"      URL: {r['url']}")
            print(f"      摘要: {r['snippet'][:100]}...")

    print(f"\n{'─' * 60}")
    print("📊 对比结论: Tavily 返回实时、具体、可验证的结果；Mock 返回预置通用文本。")


async def verify_content_fetch():
    """验证 2: 正文提取 — Jina Reader vs bs4。"""
    from app.services.content_fetcher import fetch_url

    test_urls = [
        ("中文维基", "https://zh.wikipedia.org/wiki/%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0"),
        ("英文论文", "https://arxiv.org/abs/1706.03762"),
        ("新闻文章", "https://en.wikipedia.org/wiki/Artificial_intelligence"),
    ]

    print(f"\n\n{'=' * 70}")
    print("验证 2: 网页正文提取")
    print("=" * 70)

    for label, url in test_urls:
        print(f"\n{'─' * 60}")
        print(f"📄 [{label}] URL: {url}")

        # Jina
        t0 = time.monotonic()
        jina_result = await fetch_url(url, max_chars=1500, strategy="jina")
        jina_time = (time.monotonic() - t0) * 1000

        print(f"\n  ✅ Jina Reader ({jina_time:.0f}ms, {jina_result['full_length']} chars):")
        preview = jina_result["content"][:300].replace("\n", "\n    ")
        print(f"    {preview}...")

        if jina_result.get("error"):
            print(f"    ⚠️ 错误: {jina_result['error']}")

        # bs4 fallback
        t0 = time.monotonic()
        bs4_result = await fetch_url(url, max_chars=1500, strategy="bs4")
        bs4_time = (time.monotonic() - t0) * 1000

        print(f"\n  📋 bs4 备用 ({bs4_time:.0f}ms, {bs4_result['full_length']} chars):")
        preview = bs4_result["content"][:300].replace("\n", "\n    ")
        print(f"    {preview}...")

        if bs4_result.get("error"):
            print(f"    ⚠️ 错误: {bs4_result['error']}")

    print(f"\n{'─' * 60}")
    print("📊 Jina 返回干净 Markdown，速度快；bs4 需额外清洗 HTML 噪声。")


async def verify_site_search():
    """验证 3: 定向站点抓取 — PubMed + WHO vs 通用搜索。"""
    from app.services.site_registry import search_site, list_sites
    from app.services.web_search import search_web

    topic = "COVID-19 long term effects"

    print(f"\n\n{'=' * 70}")
    print("验证 3: 定向站点抓取 vs 通用搜索")
    print(f"课题: 「{topic}」")
    print("=" * 70)

    # Show available sites
    sites = list_sites()
    print(f"\n📋 已注册站点 ({len(sites)} 个):")
    for s in sites:
        print(f"  {s['id']:20s} [{s['category']:10s}] {s['name']:20s} — {s['description']}")

    # PubMed
    print(f"\n{'─' * 60}")
    print(f"🔬 [PubMed — 医学权威数据库]")
    t0 = time.monotonic()
    pubmed_result = await search_site("pubmed", topic, num_results=3)
    pubmed_time = (time.monotonic() - t0) * 1000
    print(f"  策略: {pubmed_result['strategy']}, 耗时: {pubmed_time:.0f}ms")
    if pubmed_result.get("error"):
        print(f"  ⚠️ 错误: {pubmed_result['error']}")
    for i, r in enumerate(pubmed_result.get("results", []), 1):
        print(f"  [{i}] {r['title']}")
        print(f"      URL: {r['url']}")
        print(f"      摘要: {r['snippet'][:150]}...")

    # WHO
    print(f"\n{'─' * 60}")
    print(f"🏥 [WHO — 世界卫生组织]")
    t0 = time.monotonic()
    who_result = await search_site("who", topic, num_results=3)
    who_time = (time.monotonic() - t0) * 1000
    print(f"  策略: {who_result['strategy']}, 耗时: {who_time:.0f}ms")
    if who_result.get("error"):
        print(f"  ⚠️ 错误: {who_result['error']}")
    for i, r in enumerate(who_result.get("results", []), 1):
        print(f"  [{i}] {r['title']}")
        print(f"      URL: {r['url']}")
        print(f"      摘要: {r['snippet'][:150]}...")

    # Generic search for comparison
    print(f"\n{'─' * 60}")
    print(f"🌐 [通用搜索对比]")
    t0 = time.monotonic()
    generic_results = await search_web(topic, num_results=3)
    generic_time = (time.monotonic() - t0) * 1000
    print(f"  耗时: {generic_time:.0f}ms")
    for i, r in enumerate(generic_results, 1):
        print(f"  [{i}] {r['title']}")
        print(f"      URL: {r['url']}")
        print(f"      摘要: {r['snippet'][:150]}...")

    print(f"\n{'─' * 60}")
    print("📊 对比结论:")
    print(f"  PubMed:   直连官方 API → 结构化摘要，权威但限于学术文献")
    print(f"  WHO:      Jina 提取搜索页 → 可获得官方指南和报告")
    print(f"  通用搜索:  覆盖面广但来源混杂，需人工筛选权威性")
    print(f"  建议:     研报平台优先用定向站点，通用搜索作为补充发现渠道")


async def main():
    """Run all module 1 verifications."""
    print("🚀 Week 6 模块 1 验证：真实联网数据接入\n")

    try:
        await verify_tavily_search()
    except Exception as e:
        print(f"\n❌ 验证 1 失败: {e}")

    try:
        await verify_content_fetch()
    except Exception as e:
        print(f"\n❌ 验证 2 失败: {e}")

    try:
        await verify_site_search()
    except Exception as e:
        print(f"\n❌ 验证 3 失败: {e}")

    print(f"\n\n{'=' * 70}")
    print("✅ 模块 1 验证完成")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
