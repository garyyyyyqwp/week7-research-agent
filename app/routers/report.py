"""Report Router — SSE streaming report generation + refine + export.

Week 7 update: /generate now routes through research_pipeline.py's
run_research_pipeline (Phase 1-4), injecting real search data via
ResearchContext + CitationManager. Legacy generate_report_stream path
remains available as fallback.
"""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.utils.ratelimit import rate_limit, generation_guard

logger = logging.getLogger(__name__)

router = APIRouter(tags=["report"])


def _strip_refine_prefix(refined: str) -> str:
    """只剥离明确的解释前缀（"优化后：" / "优化结果：" 等），保留正文。

    此前的 startswith("优化") 启发式会把「优化营商环境是当前重点…」这类
    以"优化"开头的合法润色结果整行删掉 —— 内容损坏且无法恢复。
    """
    import re

    return re.sub(
        r'^\s*优化(?:后|结果|的文本|后的文字|后的结果)?[:：]\s*',
        '',
        refined,
    ).strip()

# In-memory report store (in production, use DB)
_reports: dict[str, dict] = {}
_MAX_REPORTS = 50  # Prevent memory exhaustion


# ---------------------------------------------------------------------------
# POST /api/v1/report/generate — 四阶段管道 SSE 流式生成研报 (Week 7)
# ---------------------------------------------------------------------------

@router.post("/generate", dependencies=[Depends(rate_limit(3, 60))])
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

    # 全局并发上限：每条管道占大量内存与付费配额，超限直接 429 不排队
    await generation_guard.acquire()

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
        finally:
            # 无论正常完成/异常/客户端断开，都要释放并发额度
            await generation_guard.release()

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# POST /api/v1/report/refine — 划词优化 (reused from Week 6)
# ---------------------------------------------------------------------------

@router.post("/refine", response_model=ReportRefineResponse,
             dependencies=[Depends(rate_limit(10, 60))])
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
        refined = _strip_refine_prefix(refined)
        if not refined:
            raise HTTPException(status_code=502, detail="润色服务返回为空，请重试")

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
            title = report_data.get("report", {}).get("title", "Report")

            import anyio

            # synchronous weasyprint render → anyio 线程池执行：
            # 单 worker 下同步渲染会把事件循环冻结几十秒，期间所有 SSE
            # 流、健康检查、其他请求全部停摆
            pdf_data = await anyio.to_thread.run_sync(
                _render_pdf_sync, md_content, title,
            )

            return Response(
                content=pdf_data,
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


def _fonts_dir_uri() -> str:
    """file:// URI prefix of the bundled fonts directory."""
    from pathlib import Path
    return (
        Path(__file__).resolve().parent.parent.parent / "static" / "fonts"
    ).as_uri()


def _safe_pdf_url_fetcher(url: str, timeout: int = 10, ssl_context=None):
    """weasyprint 资源抓取白名单 —— 只放行内置字体目录的 file:// URL。

    研报 markdown 源自 LLM + 抓取的网页内容，可能被注入
    <img src="file:///etc/passwd"> 或指向内网 (169.254.x.x 等) 的地址；
    weasyprint 默认会在服务端抓取这些资源（SSRF / 本地文件读取）。
    报告 PDF 不需要任何远程资源 —— 字体是唯一合法请求。
    被拒的资源 weasyprint 会记 warning 并跳过（图片显示为 alt 文本），不崩溃。
    """
    if url.startswith(_fonts_dir_uri()):
        # 延迟导入：仅放行分支需要 weasyprint（本地 Windows 无 GTK 时
        # 拦截分支仍可独立测试）
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url)
    raise ValueError(f"blocked external resource in PDF render: {url[:120]}")


def _render_pdf_sync(md_content: str, title: str) -> bytes:
    """Markdown → styled HTML → PDF bytes（同步，供线程池调用）。"""
    import io
    from weasyprint import HTML

    html = _md_to_html(md_content, title)
    buf = io.BytesIO()
    HTML(string=html, url_fetcher=_safe_pdf_url_fetcher).write_pdf(buf)
    return buf.getvalue()


def _detect_cjk_fonts() -> dict[str, str]:
    """Return {'regular': file_uri, 'bold': file_uri} of available CJK fonts.

    WeasyPrint (Pango/fontconfig) does NOT reliably pick up Windows/Linux CJK
    fonts by family name only — it walks fontconfig's registered set, and on
    a default Linux container (e.g. Render's native Python runtime) that set
    has NO CJK entries at all. apt-get is also unavailable there (non-root),
    so the only deployment-proof source is a font file bundled in the repo.
    We point Pango at that file directly via @font-face + file:// URL.

    Search order:
      1. Project-bundled fonts under <repo>/static/fonts/  ← works everywhere
      2. Common system locations (Windows / macOS / Linux)
      3. Last-resort scan: any .ttc/.otf in well-known font dirs

    Missing keys mean "not found"; an empty dict means no CJK font at all
    (CJK text would render as tofu boxes).
    """
    import glob
    import os
    import platform
    from pathlib import Path

    fonts: dict[str, str] = {}

    # 1) 项目内置字体(最可靠,任何环境都生效) — 按文件名区分字重
    project_fonts = Path(__file__).resolve().parent.parent.parent / "static" / "fonts"
    if project_fonts.is_dir():
        all_fonts = sorted(
            p for ext in ("ttf", "otf", "ttc")
            for p in project_fonts.glob(f"*.{ext}")
        )
        for p in all_fonts:
            stem = p.stem.lower()
            if "bold" in stem:
                fonts.setdefault("bold", p.as_uri())
            else:
                fonts.setdefault("regular", p.as_uri())
        if "regular" not in fonts and all_fonts:
            fonts["regular"] = all_fonts[0].as_uri()
        if fonts:
            return fonts

    system = platform.system()
    pairs: list[tuple[Path, Path | None]] = []  # (regular, bold-or-None)

    if system == "Windows":
        fdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
        pairs = [
            (fdir / "msyh.ttc", fdir / "msyhbd.ttc"),
            (fdir / "simhei.ttf", None),
            (fdir / "simsun.ttc", None),
            (fdir / "simfang.ttf", None),
            (fdir / "simkai.ttf", None),
        ]
    elif system == "Darwin":
        pairs = [
            (Path("/System/Library/Fonts/PingFang.ttc"), None),
            (Path("/System/Library/Fonts/STHeiti Light.ttc"),
             Path("/System/Library/Fonts/STHeiti Medium.ttc")),
            (Path("/Library/Fonts/Arial Unicode.ttf"), None),
        ]
    else:  # Linux / containers / Render
        pairs = [
            # fonts-noto-cjk 包的常见路径(不同 Ubuntu 版本略有差异)
            (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
             Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")),
            (Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
             Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf")),
            (Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"), None),
            (Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"), None),
            # 文泉驿
            (Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"), None),
            (Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"), None),
            # 其他可能位置
            (Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"), None),
            (Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"), None),
        ]

    for reg, bold in pairs:
        if reg.exists():
            fonts["regular"] = reg.as_uri()
            if bold is not None and bold.exists():
                fonts["bold"] = bold.as_uri()
            return fonts

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
                    fonts["regular"] = Path(matches[0]).as_uri()
                    return fonts
    return fonts


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

    # 显式注册 CJK 字体文件(Regular + Bold),只有当 Pango 真的能读到时才有意义
    fonts = _detect_cjk_fonts()
    if fonts:
        logger.info("PDF export: CJK fonts detected: %s", fonts)
    else:
        logger.warning(
            "PDF export: NO CJK font found — Chinese text will render as boxes. "
            "Bundle one under static/fonts/ (e.g. NotoSansSC-Regular.otf)."
        )

    def _font_face(url: str, weight: str) -> str:
        # .ttc 是 truetype collection,用 truetype format;扩展名推断
        ext = url.rsplit(".", 1)[-1].lower()
        fmt = "truetype" if ext in ("ttf", "ttc") else "opentype"
        return f"""
@font-face {{
  font-family: 'ProjectCJK';
  src: url('{url}') format('{fmt}');
  font-weight: {weight};
  font-style: normal;
}}
"""

    font_face_css = ""
    if "regular" in fonts:
        font_face_css += _font_face(fonts["regular"], "normal")
    if "bold" in fonts:
        font_face_css += _font_face(fonts["bold"], "bold")

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
    /* 代码里的中文注释也需要 CJK 兜底,否则同样豆腐块 */
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code",
                 "Noto Sans Mono", "ProjectCJK", "Microsoft YaHei",
                 "Noto Sans CJK SC", monospace;
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
