"""Report Schemas — Pydantic models for research reports.

Defines the structured document model:
  ResearchReport
    ├── title
    ├── abstract
    ├── sections[]
    │   ├── title
    │   ├── content (Markdown)
    │   └── citations (ref indices)
    ├── references[]
    ├── meta: ReportMeta (新增 Week 7)
    └── generated_at
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A single citation / reference entry."""
    index: int = Field(..., description="引用编号 [n]")
    url: str = Field(..., description="来源 URL")
    title: str = Field(..., description="文章/页面标题")
    snippet: str = Field(default="", description="摘要或摘要")
    source_type: str = Field(default="web", description="web, academic, official, code")
    site_name: str = Field(default="", description="来源站点名")


class ReportSection(BaseModel):
    """A single section in the report."""
    title: str = Field(..., description="章节标题")
    content: str = Field(default="", description="Markdown 格式正文")
    citations: list[int] = Field(default_factory=list, description="本节引用的引用编号")


# ---------------------------------------------------------------------------
# ReportMeta — 新增 Week 7，用于 Markdown 元数据头和导出信息
# ---------------------------------------------------------------------------

class ReportMeta(BaseModel):
    """Metadata for a generated research report."""
    topic: str = Field(..., description="研究课题")
    num_sources: int = Field(default=0, description="实际使用的来源数量")
    sites: list[str] = Field(default_factory=list, description="实际用到的站点名")
    language: str = Field(default="zh-CN", description="报告语言")
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="生成时间 (ISO 8601)",
    )
    model: str = Field(default="", description="生成用的 LLM 模型名")


class ResearchReport(BaseModel):
    """Complete structured research report."""
    title: str = Field(..., description="报告标题")
    abstract: str = Field(default="", description="摘要")
    sections: list[ReportSection] = Field(default_factory=list, description="章节列表")
    references: list[Citation] = Field(default_factory=list, description="参考文献列表")
    meta: ReportMeta | None = Field(default=None, description="报告元数据（Week 7 新增）")
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="生成时间 (ISO 8601)",
    )

    def to_markdown(self) -> str:
        """Export the report as Markdown with metadata header (PROJECT_PLAN.md §7.3)."""
        lines: list[str] = []

        # --- Metadata header (Week 7: §7.3) ---
        if self.meta:
            lines.append("---")
            lines.append(f"topic: \"{self.meta.topic}\"")
            lines.append(f"generated_at: {self.meta.generated_at}")
            lines.append(f"num_sources: {self.meta.num_sources}")
            sites_str = ", ".join(self.meta.sites)
            lines.append(f"sites: [{sites_str}]")
            lines.append(f"language: {self.meta.language}")
            lines.append(f"model: {self.meta.model}")
            lines.append("---")
            lines.append("")

        lines.append(f"# {self.title}")
        lines.append("")

        if not self.meta:
            lines.append(f"> 生成时间: {self.generated_at}")
            lines.append("")

        if self.abstract:
            lines.append("## 摘要")
            lines.append("")
            lines.append(self.abstract)
            lines.append("")

        # Table of Contents
        if self.sections:
            lines.append("## 目录")
            lines.append("")
            for i, sec in enumerate(self.sections, 1):
                lines.append(f"{i}. [{sec.title}](#{self._anchor(sec.title)})")
            lines.append("")

        # Sections
        for sec in self.sections:
            lines.append(f"## {sec.title}")
            lines.append("")
            lines.append(sec.content)
            lines.append("")
            if sec.citations:
                refs = ", ".join(f"[{c}]" for c in sec.citations)
                lines.append(f"> 本节参考文献: {refs}")
                lines.append("")

        # References
        if self.references:
            lines.append("## 参考文献")
            lines.append("")
            for ref in self.references:
                site_tag = f" *({ref.site_name})*" if ref.site_name else ""
                lines.append(f"[{ref.index}] **{ref.title}**{site_tag}")
                lines.append(f"    {ref.url}")
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _anchor(title: str) -> str:
        """Convert section title to markdown anchor."""
        return title.lower().replace(" ", "-").replace("：", "").replace(":", "")


# ---------------------------------------------------------------------------
# Report Generation Request
# ---------------------------------------------------------------------------

class ReportGenerateRequest(BaseModel):
    """Request to generate a research report (Week 7: added enabled_sites)."""
    topic: str = Field(..., min_length=1, max_length=500, description="研究主题")
    num_sections: int = Field(default=5, ge=2, le=8, description="期望章节数")
    include_references: bool = Field(default=True, description="是否包含参考文献")
    language: str = Field(default="zh-CN", description="报告语言")
    enabled_sites: list[str] = Field(
        default_factory=list,
        description="启用的定向站点列表（空=仅使用通用搜索）",
    )


class ReportGenerateResponse(BaseModel):
    """Response metadata for a generated report (for export reference)."""
    report_id: str = Field(default="", description="报告唯一 ID，用于导出端点")


class ReportRefineRequest(BaseModel):
    """Request to refine a selected text passage."""
    selected_text: str = Field(..., min_length=1, max_length=5000, description="用户选中的文字")
    context_before: str = Field(default="", max_length=2000, description="选区前文")
    context_after: str = Field(default="", max_length=2000, description="选区后文")
    instruction: str = Field(default="使这段文字更加严谨和学术化", description="优化指令")


class ReportRefineResponse(BaseModel):
    """Response from the refine endpoint."""
    refined_text: str = Field(..., description="优化后的文字")
    original_text: str = Field(default="", description="原始文字 (回显)")
    changes_summary: str = Field(default="", description="改动摘要")


# ---------------------------------------------------------------------------
# Pipeline SSE event helpers
# ---------------------------------------------------------------------------

def sse_event(event: str, data: dict) -> dict:
    """Build an SSE event dict with JSON-encoded data.

    Centralizes the JSON serialization pattern used across the pipeline.
    """
    import json
    return {
        "event": event,
        "data": json.dumps(data, ensure_ascii=False),
    }
