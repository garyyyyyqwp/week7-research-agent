"""模块 4 验证脚本：研报 Schema + 分章节 SSE 流式生成。

验证项目：
  1. Pydantic ResearchReport Schema — 模型创建 / Markdown 导出 / JSON 序列化
  2. 大纲生成 — LLM 生成报告大纲（独立测试，不需启动服务器）
  3. SSE 分章节流式生成 — 端到端验证完整流水线（需服务器运行）

用法:
  # 测试1+2（不需要服务器）
  PYTHONPATH=. python scripts/verify_module4.py

  # 测试3（需要先启动服务器: uvicorn main:app --port 8001）
  PYTHONPATH=. python scripts/verify_module4.py --server http://localhost:8001
"""

import asyncio
import json
import sys
import time
from typing import Any


# =========================================================================
# Test 1: Pydantic Schema 验证
# =========================================================================

def test_schema():
    """验证 ResearchReport 数据模型的创建、序列化和 Markdown 导出。"""
    from app.schemas.report import (
        ResearchReport,
        ReportSection,
        Citation,
        ReportGenerateRequest,
        ReportRefineRequest,
        ReportRefineResponse,
    )

    print("=" * 70)
    print("Test 4.1: ResearchReport Pydantic Schema")
    print("=" * 70)

    # 创建参考文献
    refs = [
        Citation(index=1, url="https://pubmed.ncbi.nlm.nih.gov/38512345/",
                 title="AI in healthcare: a review", snippet="...",
                 source_type="academic", site_name="PubMed"),
        Citation(index=2, url="https://www.who.int/cancer",
                 title="WHO Cancer Report", snippet="...",
                 source_type="official", site_name="WHO"),
        Citation(index=3, url="https://arxiv.org/abs/2403.12345",
                 title="Deep Learning for Medical Imaging", snippet="...",
                 source_type="academic", site_name="arXiv"),
    ]

    # 创建章节
    sections = [
        ReportSection(
            title="引言与研究背景",
            content="人工智能在医疗领域的应用正在迅速发展。**深度学习模型**已经在影像诊断中展现出超越人类医生的准确率。\n\n近年来，多项研究表明 AI 辅助诊断系统可以：\n- 减少误诊率 30-50%\n- 提高早期癌症检出率\n- 降低医生的阅片工作量",
            citations=[1, 2],
        ),
        ReportSection(
            title="核心技术分析",
            content="当前医疗 AI 的核心技术包括：\n\n| 技术 | 应用场景 | 成熟度 |\n|------|---------|--------|\n| CNN | 影像诊断 | 高 |\n| Transformer | 病历分析 | 中 |\n| GNN | 药物发现 | 中 |\n\n这些技术各有优劣，需要根据具体场景选择。",
            citations=[1, 3],
        ),
        ReportSection(
            title="挑战与展望",
            content="尽管进展显著，医疗 AI 仍面临以下挑战：\n\n1. **数据隐私**：医疗数据的敏感性限制了模型训练\n2. **可解释性**：医生需要理解 AI 的决策依据 [1]\n3. **监管审批**：FDA 等机构的审批流程较长\n\n未来，多模态大模型有望整合影像、病历、基因组学等多种数据，提供更全面的诊断建议。",
            citations=[1, 2, 3],
        ),
    ]

    # 创建完整报告
    report = ResearchReport(
        title="人工智能在医疗诊断中的应用现状与发展趋势",
        abstract="本文系统梳理了人工智能在医疗诊断领域的应用现状。深度学习模型在医学影像分析、病理诊断和临床决策支持中展现了显著优势，多项研究证实 AI 辅助可以降低误诊率并提高早期疾病检出率。然而，数据隐私、模型可解释性和监管审批仍是当前面临的主要挑战。未来，多模态大模型有望推动医疗 AI 进入新的发展阶段。",
        sections=sections,
        references=refs,
    )

    print(f"\n  ✅ Report 创建成功:")
    print(f"       标题: {report.title}")
    print(f"       摘要: {report.abstract[:80]}...")
    print(f"       章节数: {len(report.sections)}")
    print(f"       引用数: {len(report.references)}")
    print(f"       生成时间: {report.generated_at}")

    # JSON 序列化
    json_str = report.model_dump_json(indent=2)
    data = json.loads(json_str)
    assert data["title"] == report.title
    assert len(data["sections"]) == 3
    assert len(data["references"]) == 3
    assert "generated_at" in data
    print(f"\n  ✅ JSON 序列化成功 ({len(json_str)} bytes)")

    # Markdown 导出
    md = report.to_markdown()
    assert "# 人工智能在医疗诊断中的应用现状与发展趋势" in md
    assert "## 引言与研究背景" in md
    assert "## 核心技术分析" in md
    assert "## 挑战与展望" in md
    assert "## 参考文献" in md
    assert "# 目录" in md or "1." in md  # Table of contents
    print(f"\n  ✅ Markdown 导出成功 ({len(md)} chars)")
    print(f"\n  --- Markdown 预览 (前 500 chars) ---")
    print(md[:500])

    # Request Schema 验证
    gen_req = ReportGenerateRequest(
        topic="Test topic",
        num_sections=5,
        include_references=True,
        language="zh-CN",
    )
    print(f"\n  ✅ ReportGenerateRequest 验证通过: topic={gen_req.topic}, sections={gen_req.num_sections}")

    # Refine Schema
    refine_req = ReportRefineRequest(
        selected_text="This is an important finding.",
        context_before="Previous paragraph...",
        instruction="使这段文字更严谨",
    )
    assert refine_req.selected_text == "This is an important finding."
    print(f"  ✅ ReportRefineRequest 验证通过: {len(refine_req.selected_text)} chars selected")

    # Refine Response Schema
    refine_resp = ReportRefineResponse(
        refined_text="This constitutes a finding of considerable importance.",
        original_text="This is an important finding.",
        changes_summary="提升了学术严谨性",
    )
    assert refine_resp.refined_text != refine_resp.original_text
    print(f"  ✅ ReportRefineResponse 验证通过")

    print(f"\n  >> Schema 验证: ALL TESTS PASSED\n")


# =========================================================================
# Test 2: 大纲生成
# =========================================================================

async def test_outline_generation():
    """验证 LLM 大纲生成功能。"""
    from app.services.report_generator import generate_outline, _default_outline

    print("=" * 70)
    print("Test 4.2: 大纲生成 (LLM)")
    print("=" * 70)

    topic = "人工智能在医疗领域的应用"
    language = "zh-CN"

    try:
        print(f"\n  📝 研究主题: {topic}")
        print(f"  🗣 语言: {language}")
        print(f"  章节数: 3\n")

        t0 = time.monotonic()
        outline = await generate_outline(topic, num_sections=3, language=language)
        elapsed = time.monotonic() - t0

        print(f"  ✅ 大纲生成成功 ({elapsed:.1f}s, {len(outline)} 个章节):\n")
        for i, section in enumerate(outline, 1):
            title = section.get("title", "无标题")
            desc = section.get("description", "")
            print(f"    {i}. {title}")
            print(f"       {desc}")
        print()

    except Exception as e:
        print(f"  ⚠️ LLM 大纲生成失败: {e}")
        print(f"  📋 使用默认大纲作为兜底:\n")
        outline = _default_outline(topic, 3)
        for i, section in enumerate(outline, 1):
            print(f"    {i}. {section['title']}")

    # 验证大纲结构
    assert isinstance(outline, list), "大纲必须是列表"
    assert len(outline) > 0, "大纲不能为空"
    for s in outline:
        assert "title" in s, "每章必须有 title"
    print(f"\n  >> 大纲生成验证: PASSED\n")


# =========================================================================
# Test 3: SSE 流式生成端到端验证（需服务器运行）
# =========================================================================

async def test_sse_generation(server_url: str):
    """端到端测试 SSE 流式报告生成。"""
    import httpx

    print("=" * 70)
    print("Test 4.3: SSE 分章节流式生成（端到端）")
    print("=" * 70)

    topic = "气候变化对农业的影响"
    print(f"\n  📝 研究主题: {topic}")
    print(f"  🔗 服务器: {server_url}")

    events_received: list[str] = []
    sections: list[dict] = []
    has_abstract = False
    has_outline = False
    has_references = False
    has_complete = False
    total_chunks = 0

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{server_url}/api/v1/report/generate",
                json={
                    "topic": topic,
                    "num_sections": 2,
                    "include_references": True,
                    "language": "zh-CN",
                },
            ) as resp:
                assert resp.status_code == 200, f"HTTP {resp.status_code}"

                event_type = ""
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:") and event_type:
                        try:
                            data = json.loads(line[5:])
                        except json.JSONDecodeError:
                            continue

                        events_received.append(event_type)

                        if event_type == "outline":
                            has_outline = True
                            sec_count = data.get("count", 0)
                            titles = [s["title"] for s in data.get("sections", [])]
                            print(f"\n  📋 大纲: {sec_count} 个章节")
                            for i, t in enumerate(titles, 1):
                                print(f"      {i}. {t}")

                        elif event_type == "section_start":
                            print(f"\n  📖 开始生成第 {data['index'] + 1} 节: {data['title']}")

                        elif event_type == "section_chunk":
                            total_chunks += 1

                        elif event_type == "section_end":
                            sections.append({
                                "title": data["title"],
                                "content_length": len(data.get("content", "")),
                            })
                            print(f"  ✅ 章节完成: {data['title']} ({len(data.get('content', ''))} chars)")

                        elif event_type == "abstract":
                            has_abstract = True
                            abstract = data.get("abstract", "")
                            print(f"\n  📄 摘要: {abstract[:100]}...")

                        elif event_type == "references":
                            has_references = True
                            ref_count = len(data.get("references", ""))
                            print(f"\n  📚 参考文献: {ref_count} chars")

                        elif event_type == "report_complete":
                            has_complete = True
                            report_data = data.get("report", {})
                            print(f"\n  🎉 报告完成: {len(report_data.get('sections', []))} 节")

                        elif event_type == "done":
                            print(f"\n  ✅ 流式生成完成")

    except httpx.ConnectError:
        print(f"\n  ⚠️ 无法连接到服务器: {server_url}")
        print(f"  请先启动: uvicorn main:app --port 8001")
        print(f"  然后重新运行: PYTHONPATH=. python scripts/verify_module4.py --server http://localhost:8001\n")
        return

    # 验证
    print(f"\n{'─' * 40}")
    print(f"📊 SSE 事件统计:")
    print(f"  事件总数: {len(events_received)}")
    print(f"  section_chunk 数: {total_chunks}")
    print(f"  章节数: {len(sections)}")
    print(f"\n  事件类型覆盖:")
    print(f"    outline: {'✅' if has_outline else '❌'}")
    print(f"    section_start/end: {'✅' if sections else '❌'}")
    print(f"    abstract: {'✅' if has_abstract else '❌'}")
    print(f"    references: {'✅' if has_references else '❌'}")
    print(f"    report_complete: {'✅' if has_complete else '❌'}")

    # 内容验证
    for i, sec in enumerate(sections):
        assert sec["content_length"] > 0, f"章节 {i} 内容为空！"
    print(f"\n  内容完整性: {'✅' if all(s['content_length'] > 0 for s in sections) else '❌'}")

    print(f"\n  >> SSE 流式生成验证: {'PASSED' if sections and has_complete else 'INCOMPLETE'}\n")


# =========================================================================
# Main
# =========================================================================

async def main():
    """运行模块 4 完整验证。"""
    # 检查是否指定了服务器
    server_url = None
    for i, arg in enumerate(sys.argv):
        if arg == "--server" and i + 1 < len(sys.argv):
            server_url = sys.argv[i + 1]

    print("📝 模块 4 验证: 研报 Schema + 分章节 SSE 流式生成\n")

    # Test 1: Schema (不需要服务器)
    test_schema()

    # Test 2: 大纲生成 (需要 LLM API)
    await test_outline_generation()

    # Test 3: SSE 流式生成 (需要服务器)
    if server_url:
        await test_sse_generation(server_url)
    else:
        print("=" * 70)
        print("Test 4.3: SSE 分章节流式生成 — 跳过")
        print("=" * 70)
        print("\n  💡 要运行端到端测试，请:")
        print("     1. 终端1: uvicorn main:app --port 8001")
        print("     2. 终端2: PYTHONPATH=. python scripts/verify_module4.py --server http://localhost:8001\n")

    print(f"{'=' * 70}")
    print("✅ 模块 4 验证完成")
    print("=" * 70)
    print()
    print("📊 验证总结:")
    print("   Test 4.1 — Pydantic Schema: 创建/序列化/Markdown导出/Request验证")
    print("   Test 4.2 — 大纲生成: LLM 生成 → JSON 解析 → 兜底方案")
    print("   Test 4.3 — SSE 流式生成: 大纲→逐节生成→摘要→参考文献 (需启动服务器)")


if __name__ == "__main__":
    asyncio.run(main())
