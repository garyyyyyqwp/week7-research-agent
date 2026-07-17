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
            try:
                from app.schemas.report import ResearchReport
                report = ResearchReport(**report_data["report"])
                md_content = report.to_markdown()
            except Exception:
                raise HTTPException(
                    status_code=500,
                    detail="无法生成 Markdown 内容，报告数据可能已损坏。"
                )

        return Response(
            content=md_content,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=report_{report_id}.md",
            },
        )

    elif format == "pdf":
        # Use weasyprint for PDF generation; gracefully degrade on missing deps
        try:
            import weasyprint
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="PDF导出需要安装 weasyprint 及其系统依赖（cairo/pango）。"
                       "请使用 Markdown 格式导出，或在浏览器中按 Ctrl+P 打印。"
            )

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


def _detect_cjk_font_url() -> str | None:
    """Return a file:// URL of an available CJK font on the system, or None.

    WeasyPrint (Pango/fontconfig) does NOT reliably pick up Windows/Linux CJK
    fonts by family name only — it walks fontconfig's registered set, and on
    a default Linux container (e.g. Render) that set has no CJK entries. So
    we explicitly point Pango at a font file that physically exists on disk.

    Search order:
      1. Project-bundled fonts under <repo>/static/fonts/
      2. Common system locations (Windows / macOS / Linux)
      3. Last-resort scan: any .ttc/.otf in well-known font dirs
    """
    import glob
    import os
    import platform
    from pathlib import Path

    # 1) 项目内置字体(最可靠,任何环境都生效)
    project_fonts = Path(__file__).resolve().parent.parent.parent / "static" / "fonts"
    if project_fonts.is_dir():
        for ext in ("ttc", "otf", "ttf"):
            for p in sorted(project_fonts.glob(f"*.{ext}")):
                normalized = str(p).replace("\\", "/")
                if normalized.startswith("/"):
                    return f"file://{normalized}"
                return f"file:///{normalized}"

    system = platform.system()
    candidates: list[str] = []

    if system == "Windows":
        fonts_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
        for name in (
            "msyh.ttc", "msyhbd.ttc", "simhei.ttf",
            "simsun.ttc", "simfang.ttf", "simkai.ttf",
        ):
            candidates.append(os.path.join(fonts_dir, name))
    elif system == "Darwin":
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:  # Linux / containers / Render
        candidates = [
            # fonts-noto-cjk 包的常见路径(不同 Ubuntu 版本略有差异)
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            # 文泉驿
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            # 其他可能位置
            "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        ]

    for path in candidates:
        if os.path.exists(path):
            normalized = path.replace("\\", "/")
            if normalized.startswith("/"):
                return f"file://{normalized}"
            return f"file:///{normalized}"

    # 3) 最后兜底:在常见字体目录里 glob 所有 CJK 相关文件
    fallback_dirs = [
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/wqy-zenhei",
        "/usr/share/fonts/truetype/wqy",
    ]
    if system == "Windows":
        fallback_dirs.insert(
            0,
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),
        )

    for d in fallback_dirs:
        if os.path.isdir(d):
            for ext in ("ttc", "otf", "ttf"):
                matches = sorted(glob.glob(os.path.join(d, f"*.{ext}")))
                if matches:
                    normalized = matches[0].replace("\\", "/")
                    if normalized.startswith("/"):
                        return f"file://{normalized}"
                    return f"file:///{normalized}"
    return None


def _md_to_html(md_content: str, title: str = "Research Report") -> str:
    """Convert Markdown to styled HTML for PDF rendering.

    Key fix: WeasyPrint (Pango) can't auto-discover CJK fonts by family name
    on a default Windows install. We therefore load a CJK .ttf/.ttc file
    directly via @font-face with a file:// URL, so the renderer is forced
    to use it for all CJK glyphs.
    """
    import html as _html
    import markdown
    import re as _re

    safe_title = _html.escape(title)
    css_title = safe_title.replace('"', '\\"').replace("'", "\\'")

    html_body = markdown.markdown(
        md_content,
        extensions=["tables", "fenced_code", "codehilite"],
    )

    # 把 table 包一层 div,避免分页拆开
    html_body = _re.sub(r'(<table)', r'<div style="page-break-inside: avoid">\1', html_body)
    html_body = _re.sub(r'(</table>)', r'\1</div>', html_body)

    # 显式注册一个 CJK 字体文件,只有当 Pango 真的能读到时才有意义
    cjk_url = _detect_cjk_font_url()
    font_face_css = ""
    if cjk_url:
        # .ttc 是 truetype collection,用 truetype format;扩展名推断
        ext = cjk_url.rsplit(".", 1)[-1].lower()
        fmt = "truetype" if ext in ("ttf", "ttc") else "opentype"
        font_face_css = f"""
@font-face {{
  font-family: 'ProjectCJK';
  src: url('{cjk_url}') format('{fmt}');
  font-weight: normal;
  font-style: normal;
}}
"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{safe_title}</title>
<style>
  @page {{
    size: A4;
    margin: 2cm;
  }}
  {font_face_css}
  body {{
    /* 'ProjectCJK' 来自上面的 @font-face,后面是常见中文字体名兜底 */
    font-family: "ProjectCJK", "Microsoft YaHei", "微软雅黑",
                 "PingFang SC", "Heiti SC", "SimHei", "黑体",
                 "SimSun", "宋体", "Noto Sans CJK SC", "Source Han Sans CN",
                 "WenQuanYi Micro Hei", serif;
    font-size: 12pt;
    line-height: 1.8;
    color: #222;
    orphans: 3;
    widows: 3;
  }}
  h1 {{
    font-size: 20pt;
    text-align: center;
    margin-bottom: 1em;
    page-break-before: avoid;
    page-break-after: avoid;
  }}
  h2 {{
    font-size: 16pt;
    border-bottom: 2px solid #333;
    padding-bottom: 4px;
    margin-top: 1.5em;
    page-break-after: avoid;
  }}
  h3 {{ font-size: 14pt; margin-top: 1em; page-break-after: avoid; }}
  p {{ orphans: 3; widows: 3; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
    page-break-inside: avoid;
  }}
  th, td {{
    border: 1px solid #ccc;
    padding: 6px 10px;
    text-align: left;
    word-break: break-word;
  }}
  th {{ background-color: #f0f0f0; }}
  code {{
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code",
                 "Noto Sans Mono", monospace;
    background: #f5f5f5;
    padding: 1px 4px;
    font-size: 10pt;
  }}
  pre {{
    background: #f5f5f5;
    padding: 12px;
    border-radius: 4px;
    overflow-x: auto;
    page-break-inside: avoid;
  }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{
    border-left: 4px solid #ccc;
    padding-left: 1em;
    margin: 1em 0;
    color: #555;
    page-break-inside: avoid;
  }}
  img {{ max-width: 100%; }}
  ul, ol {{ page-break-inside: avoid; }}
  /* Syntax-highlighting base colors for codehilite */
  .hll {{ background-color: #ffffcc; }}
  .c {{ color: #999988; font-style: italic; }}
  .k {{ font-weight: bold; }}
  .s {{ color: #d14; }}
  .mi {{ color: #099; }}
  .mf {{ color: #099; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
