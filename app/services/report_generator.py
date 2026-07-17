"""Report Generator — 分章节 SSE 流式研报生成。

Refactored for Week 7 pipeline integration (PROJECT_PLAN.md §5.3):
  - generate_outline(): basic outline generation
  - generate_outline_with_sources(): outline with per-section source bindings
  - write_section_stream(): section writing with retrieved chunks + CM
  - generate_abstract(): standalone abstract generation
  - assemble_report(): build final ResearchReport from collected sections
  - generate_report_stream(): legacy wrapper (backward compat)

Key improvement: REAL data flows into every section via chunks from ResearchContext.
Week 6's references_text="" problem is solved by passing chunks + CM to each section.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.services.llm import get_client, get_model
from app.services.citation_manager import CitationManager
from app.utils.config import RETRIEVE_TOP_K

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

OUTLINE_PROMPT = """你是一个研究报告撰写专家。用户指定了一个研究主题，请为这个主题设计一个章节大纲。

要求:
1. 输出 JSON 格式的章节列表，每个章节包含 title（标题）和 description（简短描述这节要写什么）
2. 章节数: {num_sections} 个
3. 语言: {language}
4. 章节标题要具体、有层次、适合研究报告

只返回 JSON 数组，不要任何其他文字:
[
  {{"title": "第一章标题", "description": "本节要点"}},
  ...
]"""

# Outline prompt with source bindings — LLM assigns available sources to sections
OUTLINE_WITH_SOURCES_PROMPT = """你是一个研究报告撰写专家。用户指定了一个研究主题，请为这个主题设计一个章节大纲。

## 可用的引用来源（编号 [1] 到 [{max_index}]）
{source_list}

## 要求
1. 输出 JSON 格式的章节列表，每个章节包含:
   - title: 章节标题（具体、有层次）
   - description: 简短描述这节要写什么
   - source_indices: 该节应该引用的来源编号列表（从上面可用来源中选择最相关的）
2. 章节数: {num_sections} 个
3. 语言: {language}
4. 每个来源至少被一个章节引用

只返回 JSON 数组，不要任何其他文字:
[
  {{"title": "第一章标题", "description": "本节要点", "source_indices": [1, 3, 5]}},
  ...
]"""

SECTION_PROMPT = """你是一个研究报告撰写专家。你正在撰写一份关于「{topic}」的研究报告。

## 报告大纲
{outline_summary}

## 当前任务
撰写以下章节: **{section_title}**

## 写作要求
1. 使用学术化、严谨的语言撰写（语言: {language}）
2. **每节至少包含 1 个具体数据或案例**（如统计数字、研究结果、报道数据），不能只做笼统描述
3. 使用 Markdown 格式。**必须至少使用一处表格**来对比多种方案、多个数据点或多个研究结果
4. 适当使用 **粗体** 突出关键概念，用列表组织并列信息
5. **引用来源时使用方括号编号 [1]、[2] 等**，格式必须正确：展示数据后立即标注 [n]
6. 字数: 300-800 字
7. **不要**重复报告主标题，也不要重复其他章节已详细论述的具体观点{prior_context}

## 检索到的真实来源资料（请基于这些资料撰写，用 [n] 标注来源编号）
{research_materials}

现在开始撰写「{section_title}」的正文（不需要重复章节标题，直接写内容）："""

ABSTRACT_PROMPT = """为以下研究报告写一段摘要（200-300字）:

研究主题: {topic}

报告结构:
{sections_str}

要求:
- 语言: {language}
- 概括报告的核心内容和结论
- 直接写摘要正文，不要标题
- 控制在200-300字"""


# ---------------------------------------------------------------------------
# Public API — Outline generation
# ---------------------------------------------------------------------------

async def generate_outline(
    topic: str,
    num_sections: int = 5,
    language: str = "zh-CN",
) -> list[dict]:
    """Generate a chapter outline for the report.

    Args:
        topic: Research topic.
        num_sections: Number of sections to generate.
        language: Report language.

    Returns:
        List of {title, description} dicts.
    """
    client = get_client()
    model = get_model()

    prompt = OUTLINE_PROMPT.format(
        num_sections=num_sections,
        language=language,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的报告大纲设计助手。只输出 JSON。"},
                {"role": "user", "content": f"研究主题: {topic}\n\n{prompt}"},
            ],
            temperature=0.5,
            stream=False,
        )

        content = response.choices[0].message.content or "[]"
        sections = _parse_json_response(content)
        if not isinstance(sections, list) or len(sections) == 0:
            logger.warning("Outline parse failed, using default structure")
            return _default_outline(topic, num_sections)

        return sections

    except Exception as e:
        logger.error("Outline generation failed: %s", e)
        return _default_outline(topic, num_sections)


async def generate_outline_with_sources(
    topic: str,
    num_sections: int,
    language: str,
    cm: CitationManager,
) -> list[dict]:
    """Generate an outline with per-section source bindings.

    Uses available citations from CM to tell the LLM which sources
    are relevant to each section. Falls back to basic outline if
    no sources are available or if the LLM fails.

    Args:
        topic: Research topic.
        num_sections: Number of sections.
        language: Report language.
        cm: CitationManager with registered sources.

    Returns:
        List of {title, description, source_indices: [int]} dicts.
    """
    client = get_client()
    model = get_model()

    # Build source list for the LLM
    if cm.count == 0:
        logger.info("No sources available for outline binding, using basic outline")
        outline = await generate_outline(topic, num_sections, language)
        for sec in outline:
            sec["source_indices"] = []
        return outline

    source_list = cm.format_inline_refs()

    prompt = OUTLINE_WITH_SOURCES_PROMPT.format(
        max_index=cm.count,
        source_list=source_list,
        num_sections=num_sections,
        language=language,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的报告大纲设计助手。只输出 JSON。"},
                {"role": "user", "content": f"研究主题: {topic}\n\n{prompt}"},
            ],
            temperature=0.5,
            stream=False,
        )

        content = response.choices[0].message.content or "[]"
        sections = _parse_json_response(content)

        if not isinstance(sections, list) or len(sections) == 0:
            logger.warning("Outline-with-sources parse failed, falling back to basic outline")
            outline = await generate_outline(topic, num_sections, language)
            for sec in outline:
                sec["source_indices"] = []
            return outline

        # Ensure every section has source_indices
        for sec in sections:
            if "source_indices" not in sec:
                sec["source_indices"] = []

        return sections

    except Exception as e:
        logger.error("Outline-with-sources generation failed: %s", e)
        outline = await generate_outline(topic, num_sections, language)
        for sec in outline:
            sec["source_indices"] = []
        return outline


# ---------------------------------------------------------------------------
# Public API — Section writing
# ---------------------------------------------------------------------------

async def write_section_stream(
    topic: str,
    section: dict,
    retrieved_chunks: list[dict],
    cm: CitationManager,
    prior_summaries: list[dict] | None = None,
    language: str = "zh-CN",
    outline_summary: str = "",
) -> AsyncIterator[str]:
    """Write a single section with streaming output.

    Injects retrieved research chunks into the prompt so the LLM writes
    based on REAL data, not from its own knowledge. This is the core fix
    for Week 6's "LLM编造" problem.

    Args:
        topic: Research topic.
        section: Dict with 'title', 'description', optionally 'source_indices'.
        retrieved_chunks: Chunks from ResearchContext.retrieve().
        cm: CitationManager for formatting available references.
        prior_summaries: Key sentences from previously written sections.
        language: Report language.
        outline_summary: Pre-built outline summary string.

    Yields:
        Text chunks (str) as the LLM streams them.
    """
    client = get_client()
    model = get_model()
    section_title = section.get("title", "Untitled")

    # Build research materials section from retrieved chunks
    research_materials = _format_research_materials(retrieved_chunks, cm)

    # Build prior context to prevent repetition
    prior_context = ""
    if prior_summaries:
        prior_context = "\n\n## 已完成章节的关键要点（避免重复）\n"
        for ps in prior_summaries:
            prior_context += f"- **{ps['title']}**: {ps['key']}\n"

    prompt = SECTION_PROMPT.format(
        topic=topic,
        outline_summary=outline_summary,
        section_title=section_title,
        language=language,
        research_materials=research_materials,
        prior_context=prior_context,
    )

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的学术研究报告撰写助手。请基于提供的真实资料撰写，引用时使用 [n] 格式标注来源编号。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            stream=True,
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    except Exception as e:
        logger.error("Section generation failed for '%s': %s", section_title, e)
        yield f"\n*(本节生成时遇到错误: {e})*\n"


# ---------------------------------------------------------------------------
# Public API — Abstract
# ---------------------------------------------------------------------------

async def generate_abstract(
    topic: str,
    outline: list[dict],
    language: str = "zh-CN",
) -> str:
    """Generate an abstract for the report.

    Args:
        topic: Research topic.
        outline: List of section dicts.
        language: Report language.

    Returns:
        Abstract text.
    """
    client = get_client()
    model = get_model()

    sections_str = "\n".join(f"- {s['title']}" for s in outline)

    prompt = ABSTRACT_PROMPT.format(
        topic=topic,
        sections_str=sections_str,
        language=language,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的学术摘要撰写助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            stream=False,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("Abstract generation failed: %s", e)
        return f"本报告系统研究了「{topic}」相关领域的发展现状、关键问题与未来趋势。"


# ---------------------------------------------------------------------------
# Public API — Report assembly
# ---------------------------------------------------------------------------

def assemble_report(
    topic: str,
    abstract: str,
    sections_data: list[dict],
    cm: CitationManager,
    meta: dict | None = None,
) -> Any:
    """Build a ResearchReport from collected sections and citations.

    Args:
        topic: Report title.
        abstract: Generated abstract.
        sections_data: List of {title, content, citations} dicts.
        cm: CitationManager with registered sources.
        meta: Optional metadata dict for ReportMeta.

    Returns:
        ResearchReport instance.
    """
    from app.schemas.report import (
        ResearchReport,
        ReportSection,
        Citation,
        ReportMeta,
    )

    # Build citation list from CM
    citations = []
    for c in cm.sources:
        citations.append(Citation(
            index=c.index,
            url=c.url,
            title=c.title,
            snippet=c.snippet,
            source_type=c.source_type,
            site_name=c.site_name,
        ))

    # Build sections
    sections = []
    for sd in sections_data:
        sections.append(ReportSection(
            title=sd["title"],
            content=sd.get("content", ""),
            citations=sd.get("citations", []),
        ))

    # Build meta
    report_meta = None
    if meta:
        report_meta = ReportMeta(
            topic=meta.get("topic", topic),
            num_sources=meta.get("num_sources", cm.count),
            sites=meta.get("sites", []),
            language=meta.get("language", "zh-CN"),
            generated_at=meta.get("generated_at", datetime.now(timezone.utc).isoformat()),
            model=meta.get("model", get_model()),
        )

    return ResearchReport(
        title=topic,
        abstract=abstract,
        sections=sections,
        references=citations,
        meta=report_meta,
    )


def extract_citation_indices(text: str, max_index: int | None = None) -> list[int]:
    """Extract citation indices like [1], [2,3], [4-6] from section text.

    Used to populate section.citations in the SSE section_end event.

    Args:
        text: Section text possibly containing [n] markers.
        max_index: If given, only 1 <= idx <= max_index are kept — filters
            out year spans like [2021-2025], scale cells like [0-100], and
            hallucinated citation numbers beyond the registered sources.
    """
    indices: set[int] = set()
    # Match [n] or [n,m] or [n-m]
    patterns = re.findall(r'\[([^\]]+)\]', text)
    for p in patterns:
        # Try single number
        for part in p.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    a, b = part.split("-", 1)
                    a_i, b_i = int(a), int(b)
                    # 拒绝异常宽的区间（如年份 2021-2025 / 量表 0-100）
                    if b_i - a_i > 20:
                        continue
                    indices.update(range(a_i, b_i + 1))
                except ValueError:
                    pass
            else:
                try:
                    indices.add(int(part))
                except ValueError:
                    pass

    if max_index is not None:
        indices = {i for i in indices if 1 <= i <= max_index}
    else:
        indices = {i for i in indices if i >= 1}
    return sorted(indices)


# ---------------------------------------------------------------------------
# Legacy wrapper — backwards-compatible with Week 6
# ---------------------------------------------------------------------------

async def generate_report_stream(
    topic: str,
    num_sections: int = 5,
    include_references: bool = True,
    language: str = "zh-CN",
) -> AsyncIterator[dict]:
    """Generate a full research report with SSE streaming (legacy mode).

    This is the Week 6 compatible wrapper. It does NOT use real search data —
    for real data, use research_pipeline.py instead.

    SSE Events:
      outline → section_start → section_chunk (×N) → section_end
      → (next section_start...) → references → done
    """
    client = get_client()
    model = get_model()

    # Step 1: Generate outline
    yield _sse_old("status", {"status": "outline", "message": "正在生成报告大纲..."})

    outline = await generate_outline(topic, num_sections, language)

    yield _sse_old("outline", {
        "topic": topic,
        "sections": outline,
        "count": len(outline),
    })

    # Build outline summary for section prompt
    outline_summary = "\n".join(
        f"{i+1}. **{s['title']}**: {s.get('description', '')}"
        for i, s in enumerate(outline)
    )

    # Step 2: Generate each section
    all_sections_content = []
    cm = CitationManager()

    old_section_prompt = (
        SECTION_PROMPT
        .replace("{research_materials}", "(本节暂无引用来源)")
        .replace("{prior_context}", "")
    )

    for i, section_info in enumerate(outline):
        section_title = section_info["title"]

        yield _sse_old("section_start", {
            "index": i,
            "title": section_title,
            "total": len(outline),
        })

        # Build section prompt
        previous_sections = ""
        if all_sections_content:
            prev_summary = "\n".join(
                f"### {s['title']}\n{s['content'][:200]}..."
                for s in all_sections_content
            )
            previous_sections = f"\n## 已完成的章节\n{prev_summary}\n"

        prompt = old_section_prompt.format(
            topic=topic,
            outline_summary=outline_summary + previous_sections,
            section_title=section_title,
            language=language,
        )

        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个专业的学术研究报告撰写助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.6,
                stream=True,
            )

            collected = ""
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    collected += delta.content
                    yield _sse_old("section_chunk", {
                        "index": i,
                        "chunk": delta.content,
                    })

        except Exception as e:
            logger.error("Section generation failed: %s", e)
            collected = f"*(本节生成失败: {str(e)})*"
            yield _sse_old("section_chunk", {
                "index": i,
                "chunk": collected,
            })

        all_sections_content.append({
            "title": section_title,
            "content": collected,
        })

        yield _sse_old("section_end", {
            "index": i,
            "title": section_title,
            "content": collected,
            "citations": [],
        })

    # Step 3: Abstract
    yield _sse_old("status", {"status": "abstract", "message": "正在生成摘要..."})

    abstract = await generate_abstract(topic, outline, language)

    yield _sse_old("abstract", {"abstract": abstract})

    # Step 4: References
    if include_references and cm.count > 0:
        yield _sse_old("references", {
            "references": cm.format_references(),
            "citations_json": cm.to_dict(),
        })

    # Step 5: Build final report
    from app.schemas.report import ResearchReport, ReportSection

    report = ResearchReport(
        title=topic,
        abstract=abstract,
        sections=[
            ReportSection(
                title=s["title"],
                content=s["content"],
                citations=[],
            )
            for s in all_sections_content
        ],
        references=[],
    )

    yield _sse_old("report_complete", {
        "report": report.model_dump(),
        "markdown": report.to_markdown(),
    })

    yield _sse_old("done", {
        "topic": topic,
        "sections_count": len(all_sections_content),
    })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_outline(topic: str, num_sections: int) -> list[dict]:
    """Fallback outline structure."""
    defaults = [
        {"title": "引言与研究背景", "description": "介绍研究主题的背景和意义"},
        {"title": "核心概念与理论基础", "description": "梳理关键概念和理论框架"},
        {"title": "当前研究现状分析", "description": "综述最新研究成果和进展"},
        {"title": "关键问题与挑战", "description": "分析面临的主要问题和挑战"},
        {"title": "发展趋势与展望", "description": "展望未来研究方向和应用前景"},
    ]
    return defaults[:num_sections]


def _parse_json_response(content: str) -> Any:
    """Extract and parse JSON from LLM response, handling markdown code fences.

    Uses bracket-depth tracking to correctly extract JSON arrays from responses
    that may have trailing text with bracket characters.
    """
    # Remove markdown code fences first
    cleaned = re.sub(r'```(?:json)?\s*', '', content)
    cleaned = re.sub(r'```\s*', '', cleaned)

    # Find the first '[' and track depth to find matching ']'
    start = cleaned.find('[')
    if start == -1:
        raise json.JSONDecodeError("No JSON array found", content, 0)

    depth = 0
    in_string = False
    escape_next = False
    end = -1
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        raise json.JSONDecodeError("Unmatched bracket in JSON", cleaned, start)

    extracted = cleaned[start:end + 1]
    return json.loads(extracted)


def _sse_old(event: str, data: dict) -> dict:
    """Build an SSE event dict (legacy compat)."""
    return {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False),
    }


def _format_research_materials(
    chunks: list[dict],
    cm: CitationManager,
) -> str:
    """Format retrieved research chunks for the section prompt.

    Each chunk gets labeled with its source's citation number (from CM)
    so the LLM can use the correct [n] reference.
    """
    if not chunks:
        return "(暂无相关检索资料)"

    # Build URL → citation index map
    url_to_idx: dict[str, int] = {}
    for c in cm.sources:
        url_to_idx[c.url] = c.index

    lines = []
    for i, chunk in enumerate(chunks):
        url = chunk.get("url", "")
        site = chunk.get("site", "")
        title = chunk.get("title", "")
        cit_idx = url_to_idx.get(url, 0)

        ref_label = f"[{cit_idx}]" if cit_idx > 0 else f"[来源{i+1}]"
        lines.append(f"--- 资料片段 {ref_label} ---")
        lines.append(f"来源: {site} — {title}")
        lines.append(f"URL: {url}")
        lines.append(chunk.get("content", ""))
        lines.append("")

    return "\n".join(lines)
