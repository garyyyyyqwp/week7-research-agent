"""Report Router — SSE streaming report generation + refine + export.

Week 7 update: /generate now routes through research_pipeline.py's
run_research_pipeline (Phase 1-4), injecting real search data via
ResearchContext + CitationManager. Legacy generate_report_stream path
remains available as fallback.
"""

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from sse_starlette.sse import EventSourceResponse

from app.schemas.report import (
    ReportGenerateRequest,
    ReportRefineRequest,
    ReportRefineResponse,
)
from app.services.report_generator import generate_report_stream
from app.services.research_pipeline import run_research_pipeline
from app.services.llm import get_client, get_model

logger = logging.getLogger(__name__)

router = APIRouter(tags=["report"])

# In-memory report store (in production, use DB)
_reports: dict[str, dict] = {}
_MAX_REPORTS = 50  # Prevent memory exhaustion


# ---------------------------------------------------------------------------
# POST /api/v1/report/generate — 四阶段管道 SSE 流式生成研报 (Week 7)
# ---------------------------------------------------------------------------

@router.post("/generate")
async def generate_report(request: ReportGenerateRequest):
    """Generate a research report via the 4-phase research pipeline.

    SSE Events (PROJECT_PLAN.md §7.2):
        research_start → research_progress (×N) → research_done
        → [warning] → outline → section_start → section_chunk (×N)
        → section_end → ... → abstract → references → report_complete → done
        → [error]

    Uses run_research_pipeline which:
      - Searches real sources via Tavily/directed sites
      - Stores full text in ResearchContext (ChromaDB)
      - Retrieves top_k chunks per section (RAG)
      - Writes sections with real citations ([n] format)
    """
    report_id = uuid.uuid4().hex[:12]

    async def event_generator():
        try:
            async for event in run_research_pipeline(
                report_id=report_id,
                topic=request.topic,
                num_sections=request.num_sections,
                language=request.language,
                enabled_sites=request.enabled_sites,
            ):
                # Capture report data for later export
                if event.get("event") == "report_complete":
                    try:
                        data = json.loads(event["data"])
                        _reports[report_id] = data
                        # Ensure report_id is present
                        data["report_id"] = report_id
                        event["data"] = json.dumps(data, ensure_ascii=False)
                        # Prune old reports if over limit
                        if len(_reports) > _MAX_REPORTS:
                            oldest = next(iter(_reports))
                            del _reports[oldest]
                    except Exception:
                        pass
                yield event
        except Exception as e:
            logger.error("Report generation error: %s", e, exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps({
                    "message": "研报生成失败，请重试",
                    "phase": "generate",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# POST /api/v1/report/refine — 划词优化 (reused from Week 6)
# ---------------------------------------------------------------------------

@router.post("/refine", response_model=ReportRefineResponse)
async def refine_text(request: ReportRefineRequest):
    """Refine a selected text passage with LLM assistance.

    Receives the selected text along with surrounding context, applies the
    user's instruction (e.g., "make this more rigorous"), and returns the
    refined text for the frontend to replace.
    """
    client = get_client()
    model = get_model()

    prompt = f"""你是一个学术文字润色助手。用户选中了一段报告中的文字，请你按照要求优化。

## 上下文（供参考，不需要修改）
前文: {request.context_before or "(无)"}

后文: {request.context_after or "(无)"}

## 需要优化的文字
{request.selected_text}

## 用户要求
{request.instruction}

## 要求
1. 只返回优化后的文字内容
2. 不要添加任何解释、说明或前缀
3. 保持原意，只优化表达方式
4. 使用与上下文一致的风格和术语"""

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的学术文字润色助手。只返回润色后的文字，不加任何解释。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            stream=False,
        )

        refined = (response.choices[0].message.content or "").strip()

        # If LLM prepended explanation, strip it
        if refined.startswith("优化"):
            lines = refined.split("\n")
            # Find the first non-empty line that might be the start of content
            refined = "\n".join(lines[1:]).strip()
            if not refined:
                refined = lines[0]

        return ReportRefineResponse(
            refined_text=refined,
            original_text=request.selected_text,
            changes_summary=f"根据「{request.instruction}」进行了优化",
        )

    except Exception as e:
        logger.error("Refine error: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="润色服务暂时不可用，请稍后重试")


# ---------------------------------------------------------------------------
# GET /api/v1/report/{report_id}/export — 文档导出
# ---------------------------------------------------------------------------

@router.get("/{report_id}/export")
async def export_report(
    report_id: str,
    format: str = Query("md", description="Export format: md or pdf"),
):
    """Export a generated report as Markdown or PDF."""
    report_data = _reports.get(report_id)
    if not report_data:
        raise HTTPException(
            status_code=404,
            detail=f"报告不存在: {report_id}。请先生成报告。"
        )

    if format == "md":
        md_content = report_data.get("markdown", "")
        if not md_content:
            # Generate markdown from report JSON
            from app.schemas.report import ResearchReport
            report = ResearchReport(**report_data["report"])
            md_content = report.to_markdown()

        return Response(
            content=md_content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=report_{report_id}.md",
            },
        )

    elif format == "pdf":
        # Use weasyprint for PDF generation
        try:
            md_content = report_data.get("markdown", "")
            html = _md_to_html(md_content, report_data.get("report", {}).get("title", "Report"))

            from weasyprint import HTML
            import io

            pdf_bytes = io.BytesIO()
            HTML(string=html).write_pdf(pdf_bytes)
            pdf_bytes.seek(0)

            return Response(
                content=pdf_bytes.getvalue(),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename=report_{report_id}.pdf",
                },
            )
        except Exception as e:
            logger.error("PDF export error: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="PDF导出失败，请尝试 Markdown 格式导出。"
            )

    else:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导出格式: {format}。支持: md, pdf"
        )


def _md_to_html(md_content: str, title: str = "Research Report") -> str:
    """Convert Markdown to styled HTML for PDF rendering."""
    import html as _html
    import markdown

    safe_title = _html.escape(title)

    html_body = markdown.markdown(
        md_content,
        extensions=["tables", "fenced_code", "codehilite"],
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{safe_title}</title>
<style>
  @page {{
    size: A4;
    margin: 2cm;
    @top-center {{
      content: "{safe_title}";
      font-size: 10pt;
      color: #666;
    }}
  }}
  body {{
    font-family: "SimSun", "Noto Sans CJK SC", "Source Han Sans CN", serif;
    font-size: 12pt;
    line-height: 1.8;
    color: #222;
  }}
  h1 {{ font-size: 20pt; text-align: center; margin-bottom: 1em; }}
  h2 {{ font-size: 16pt; border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 1.5em; }}
  h3 {{ font-size: 14pt; margin-top: 1em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
  th {{ background-color: #f0f0f0; }}
  code {{ background: #f5f5f5; padding: 1px 4px; font-size: 10pt; }}
  pre {{ background: #f5f5f5; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  blockquote {{ border-left: 4px solid #ccc; padding-left: 1em; color: #555; }}
  img {{ max-width: 100%; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
