"""Citation Tracking — 引用追踪与参考文献生成。

Tracks every information source the agent uses, injects inline citation
markers ([1], [2], ...) into generated text, and formats a reference list.

Usage in agent loop:
    cm = CitationManager()
    cm.add("https://pubmed.ncbi.nlm.nih.gov/12345/", "Paper Title", "Abstract...")
    # ... after LLM generates text with [1] markers ...
    references = cm.format_references()
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class Citation:
    """A single citation / reference entry."""
    index: int
    url: str
    title: str
    snippet: str = ""
    source_type: str = "web"       # "web", "academic", "official", "code"
    site_name: str = ""            # e.g., "PubMed", "arXiv", "WHO"
    fetched_at: str = ""


class CitationManager:
    """Manages citations for a research report.

    Thread-safe for in-memory operations. Each instance tracks one report's
    citations. Citation numbers are 1-indexed.

    Usage:
        cm = CitationManager()

        # Register sources as the agent searches
        idx = cm.add("https://pubmed.ncbi.nlm.nih.gov/12345/",
                      "Long COVID study", "Background: ...")
        print(idx)  # 1

        # After LLM generates answer with citation markers
        text = cm.inject_inline("COVID can cause fatigue.[1] Lung damage is common.[2]")

        # Format the full reference list
        refs = cm.format_references()
        # [1] Long COVID study — https://pubmed.ncbi.nlm.nih.gov/12345/
    """

    def __init__(self):
        self._sources: dict[str, Citation] = {}  # URL -> Citation
        self._counter: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        url: str,
        title: str,
        snippet: str = "",
        source_type: str = "web",
        site_name: str = "",
    ) -> int:
        """Register a source and return its citation number.

        If the same URL is already registered, returns the existing number.
        Empty URLs are rejected to prevent data-quality issues.

        Args:
            url: Source URL (must be non-empty).
            title: Title of the article/page.
            snippet: Short summary or abstract.
            source_type: Category ("web", "academic", "official", "code").
            site_name: Source site name (e.g., "PubMed", "arXiv").

        Returns:
            Citation number (1-indexed), or 0 if URL is empty.
        """
        if not url or not url.strip():
            logger.warning("CitationManager.add: empty URL rejected (title=%s)", title[:60])
            return 0

        if url in self._sources:
            return self._sources[url].index

        self._counter += 1
        self._sources[url] = Citation(
            index=self._counter,
            url=url,
            title=title,
            snippet=snippet,
            source_type=source_type,
            site_name=site_name,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        return self._counter

    def add_batch(self, sources: list[dict[str, Any]]) -> list[int]:
        """Register multiple sources at once.

        Args:
            sources: List of dicts with 'url', 'title', 'snippet', etc.

        Returns:
            List of citation numbers for each source.
        """
        indices = []
        for src in sources:
            idx = self.add(
                url=src.get("url", ""),
                title=src.get("title", ""),
                snippet=src.get("snippet", ""),
                source_type=src.get("source_type", "web"),
                site_name=src.get("site_name", ""),
            )
            indices.append(idx)
        return indices

    def get_by_url(self, url: str) -> Citation | None:
        """Look up a citation by its URL."""
        return self._sources.get(url)

    def get_by_index(self, index: int) -> Citation | None:
        """Look up a citation by its index number."""
        for c in self._sources.values():
            if c.index == index:
                return c
        return None

    def inject_inline(self, text: str) -> str:
        """Ensure citation markers [n] appear in the text.

        This is a pass-through — the LLM is expected to output text with
        [1], [2] markers already in place. This method validates that
        referenced indices exist.

        Args:
            text: Text that may contain citation markers [n].

        Returns:
            The text unchanged (validation pass-through).
        """
        # Future: auto-insert markers using NLP entity detection
        return text

    def format_references(self, style: str = "markdown") -> str:
        """Generate a formatted reference list.

        Args:
            style: "markdown" (default) or "plain".

        Returns:
            Formatted reference list as a string.
        """
        if not self._sources:
            return ""

        sorted_citations = sorted(
            self._sources.values(), key=lambda c: c.index
        )

        if style == "markdown":
            lines = ["## 📚 参考文献\n"]
            for c in sorted_citations:
                source_badge = f" `[{c.source_type}]`" if c.source_type else ""
                site_prefix = f"*{c.site_name}* — " if c.site_name else ""
                lines.append(
                    f"[{c.index}] {site_prefix}"
                    f"**{c.title}**{source_badge}\n"
                    f"    {c.url}"
                )
            return "\n".join(lines)

        elif style == "plain":
            lines = ["参考文献:"]
            for c in sorted_citations:
                lines.append(f"[{c.index}] {c.title} — {c.url}")
            return "\n".join(lines)

        return ""

    def format_inline_refs(self) -> str:
        """Generate a compact inline reference summary for LLM system prompt.

        The LLM uses this to know which citation numbers correspond to
        which sources when writing its answer.

        Returns:
            String like "[1] Paper Title\n[2] Another Paper"
        """
        if not self._sources:
            return ""

        lines = ["## 可用引用来源"]
        for c in sorted(self._sources.values(), key=lambda c: c.index):
            lines.append(f"[{c.index}] {c.title} — {c.url}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize all citations to a JSON-serializable dict."""
        return {
            "count": self._counter,
            "citations": [
                {
                    "index": c.index,
                    "url": c.url,
                    "title": c.title,
                    "snippet": c.snippet,
                    "source_type": c.source_type,
                    "site_name": c.site_name,
                    "fetched_at": c.fetched_at,
                }
                for c in sorted(self._sources.values(), key=lambda c: c.index)
            ],
        }

    @property
    def count(self) -> int:
        """Number of registered citations."""
        return self._counter

    @property
    def sources(self) -> list[Citation]:
        """All registered citations sorted by index."""
        return sorted(self._sources.values(), key=lambda c: c.index)
