"""Research Pipeline — 四阶段编排（Week 7 核心引擎）。

Single async generator that chains Phase 1-4, holding the ONE CitationManager
and ONE ResearchContext throughout the entire pipeline. This is the architectural
centerpiece that eliminates Week 6's "two CM + LLM fabricates everything" problem.

Phase 1 → Research (ResearchEngine)
Phase 2 → Outline (generate_outline_with_sources)
Phase 3 → Sections (write_section_stream per section, with RC.retrieve() chunks)
Phase 4 → Post-processing (abstract + references + report assembly)

Design invariants (PROJECT_PLAN.md §4.4):
  - A single CM+RC pair lives start-to-finish for one report
  - Per-section prompts only receive top_k chunks (never full source text)
  - finally: rc.cleanup() ALWAYS runs, even on exception
  - SSE event names and data fields match §7.2 contract exactly

See PROJECT_PLAN.md §5.3 for full specification.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.services.citation_manager import CitationManager
from app.services.research_context import ResearchContext
from app.services.research_engine import ResearchEngine
from app.services.report_generator import (
    generate_outline_with_sources,
    write_section_stream,
    generate_abstract,
    assemble_report,
    extract_citation_indices,
)
from app.services.llm import get_model
from app.utils.config import (
    AGENT_MAX_STEPS,
    RETRIEVE_TOP_K,
    MIN_SOURCES,
)

logger = logging.getLogger(__name__)


async def run_research_pipeline(
    report_id: str,
    topic: str,
    num_sections: int = 5,
    language: str = "zh-CN",
    enabled_sites: list[str] | None = None,
) -> AsyncIterator[dict]:
    """Execute the full 4-phase research pipeline as an SSE event stream.

    This is the primary entry point called by the /report/generate router.
    One call = one complete report generation cycle.

    Args:
        report_id: Unique ID for this report (used as RC namespace).
        topic: Research topic (1-500 chars).
        num_sections: Number of sections (2-8).
        language: Report language ("zh-CN" or "en").
        enabled_sites: List of site IDs for directed search (empty = Tavily only).

    Yields:
        SSE event dicts following the §7.2 protocol.
    """
    enabled_sites = enabled_sites or []
    pipeline_start = time.monotonic()
    cm = CitationManager()
    rc = ResearchContext(report_id)

    # Track sources used (URL → site name)
    used_sites: set[str] = set()
    # Track outline sections for the report
    outline_sections: list[dict] = []

    try:
        # =====================================================================
        # Phase 1: Research
        # =====================================================================
        yield _sse("research_start", {
            "topic": topic,
            "sites": enabled_sites,
        })

        engine = ResearchEngine(cm, rc, max_steps=AGENT_MAX_STEPS)
        async for ev in engine.research(topic, enabled_sites):
            yield ev
            # Collect site names from progress events for metadata
            data = _parse_event_data(ev)
            # This is handled later via CM sources

        # Collect site names from registered citations
        for c in cm.sources:
            if c.site_name:
                used_sites.add(c.site_name)

        research_elapsed = time.monotonic() - pipeline_start
        yield _sse("research_done", {
            "sources": cm.count,
            "elapsed_s": round(research_elapsed, 1),
        })

        logger.info(
            "Phase 1 complete: %d sources, %d sites, %.1fs",
            cm.count, len(used_sites), research_elapsed,
        )

        # --- Warning: few sources ---
        if cm.count < MIN_SOURCES:
            yield _sse("warning", {
                "code": "few_sources",
                "count": cm.count,
                "message": f"数据来源较少（{cm.count} 个），内容可能不够全面",
            })

        # =====================================================================
        # Phase 2: Outline with source bindings
        # =====================================================================
        outline = await generate_outline_with_sources(
            topic=topic,
            num_sections=num_sections,
            language=language,
            cm=cm,
        )
        outline_sections = outline

        yield _sse("outline", {
            "topic": topic,
            "sections": outline,
            "count": len(outline),
        })

        logger.info(
            "Phase 2 complete: %d sections in outline",
            len(outline),
        )

        # =====================================================================
        # Phase 3: Section Writing (with RAG)
        # =====================================================================
        outline_summary = "\n".join(
            f"{i+1}. **{s['title']}**: {s.get('description', '')}"
            for i, s in enumerate(outline)
        )

        prior_summaries: list[dict] = []
        all_sections_content: list[dict] = []

        for i, sec in enumerate(outline):
            section_title = sec.get("title", f"Section {i+1}")

            yield _sse("section_start", {
                "index": i,
                "title": section_title,
                "total": len(outline),
            })

            # --- ★ CORE FIX: retrieve only top_k relevant chunks ★ ---
            # This replaces Week 6's references_text="" with real data
            chunks = await rc.retrieve(section_title, top_k=RETRIEVE_TOP_K)

            # Estimate token usage for logging
            chunk_tokens_est = sum(len(c.get("content", "")) // 4 for c in chunks)
            logger.info(
                "Section %d '%s': retrieved %d chunks (~%d tokens)",
                i + 1, section_title, len(chunks), chunk_tokens_est,
            )

            # Stream-write the section
            collected = ""
            try:
                async for text_chunk in write_section_stream(
                    topic=topic,
                    section=sec,
                    retrieved_chunks=chunks,
                    cm=cm,
                    prior_summaries=prior_summaries,
                    language=language,
                    outline_summary=outline_summary,
                ):
                    collected += text_chunk
                    yield _sse("section_chunk", {
                        "index": i,
                        "chunk": text_chunk,
                    })
            except Exception as e:
                logger.error(
                    "Section '%s' generation failed: %s", section_title, e,
                )
                collected += f"\n*(本节生成时遇到错误: {e})*\n"

            # Extract citation indices from generated text
            used_citations = extract_citation_indices(collected)

            yield _sse("section_end", {
                "index": i,
                "title": section_title,
                "content": collected,
                "citations": used_citations,
            })

            all_sections_content.append({
                "title": section_title,
                "content": collected,
                "citations": used_citations,
            })

            # Store key sentences for anti-repetition in subsequent sections
            prior_summaries.append({
                "title": section_title,
                "key": _first_sentences(collected, 2),
            })

        section_elapsed = time.monotonic() - pipeline_start
        logger.info(
            "Phase 3 complete: %d sections written in %.1fs",
            len(all_sections_content), section_elapsed,
        )

        # =====================================================================
        # Phase 4: Post-processing
        # =====================================================================
        abstract = await generate_abstract(topic, outline, language)
        yield _sse("abstract", {"abstract": abstract})

        yield _sse("references", {
            "references": cm.format_references(),
            "citations_json": cm.to_dict(),
        })

        # Assemble the final report
        report = assemble_report(
            topic=topic,
            abstract=abstract,
            sections_data=all_sections_content,
            cm=cm,
            meta={
                "topic": topic,
                "num_sources": cm.count,
                "sites": sorted(used_sites) if used_sites else ["Tavily"],
                "language": language,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": get_model(),
            },
        )

        yield _sse("report_complete", {
            "report": report.model_dump(),
            "markdown": report.to_markdown(),
            "report_id": report_id,
        })

        total_elapsed = time.monotonic() - pipeline_start
        yield _sse("done", {
            "report_id": report_id,
            "sources": cm.count,
            "sections": len(all_sections_content),
            "elapsed_s": round(total_elapsed, 1),
        })

        logger.info(
            "Pipeline complete: report_id=%s, sources=%d, sections=%d, "
            "sites=%s, elapsed=%.1fs",
            report_id, cm.count, len(all_sections_content),
            sorted(used_sites) if used_sites else ["Tavily"],
            total_elapsed,
        )

    except Exception as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        yield _sse("error", {
            "message": "研报生成失败，请重试",
            "phase": "pipeline",
            "detail": str(e)[:200],
        })
    finally:
        # ★ ALWAYS cleanup — prevents memory leaks (PROJECT_PLAN.md §4.4) ★
        rc.cleanup()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> dict:
    """Build an SSE event dict with JSON-encoded data."""
    return {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False),
    }


def _parse_event_data(ev: dict) -> dict:
    """Parse the data field of an SSE event dict."""
    try:
        return json.loads(ev.get("data", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}


def _first_sentences(text: str, n: int = 2) -> str:
    """Extract the first n sentences from text for anti-repetition summary."""
    if not text:
        return ""
    # Split on sentence boundaries for both Chinese and English
    import re
    sentences = re.split(r'[。！？!?.\n]', text)
    result = []
    count = 0
    for s in sentences:
        s = s.strip()
        if s and len(s) > 10:  # Skip very short fragments
            result.append(s)
            count += 1
            if count >= n:
                break
    return "。".join(result) + "。" if result else text[:200]
