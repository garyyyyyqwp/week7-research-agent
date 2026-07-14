"""Test citation flow — verify citation number consistency across the pipeline.

Verifies (PROJECT_PLAN.md DoD for Task B):
  1. Same URL de-duplicates → returns same citation index
  2. Phase 1 registered indices match Phase 4 references
  3. extract_citation_indices() correctly parses [n], [n,m], [n-m] patterns
  4. A single CitationManager instance maintains consistency throughout
"""

import pytest

from app.services.citation_manager import CitationManager
from app.services.report_generator import extract_citation_indices


# ---------------------------------------------------------------------------
# CitationManager consistency tests
# ---------------------------------------------------------------------------

class TestCitationConsistency:
    """Verify CM dedup and index consistency."""

    def test_add_same_url_returns_same_index(self):
        cm = CitationManager()
        idx1 = cm.add("https://pubmed.ncbi.nlm.nih.gov/12345/", "Paper A", "abstract")
        idx2 = cm.add("https://pubmed.ncbi.nlm.nih.gov/12345/", "Paper A again", "different abstract")
        assert idx1 == idx2, (
            f"Same URL should return same index: {idx1} != {idx2}"
        )
        assert cm.count == 1

    def test_add_different_urls_sequential_indices(self):
        cm = CitationManager()
        idx1 = cm.add("https://a.com", "A", "")
        idx2 = cm.add("https://b.com", "B", "")
        idx3 = cm.add("https://c.com", "C", "")
        assert idx1 == 1
        assert idx2 == 2
        assert idx3 == 3
        assert cm.count == 3

    def test_phase1_indices_present_in_phase4_references(self):
        """Simulate: Phase 1 registers sources, Phase 4 formats references."""
        cm = CitationManager()

        # Phase 1: register sources (as agent tools would)
        urls = [
            "https://pubmed.ncbi.nlm.nih.gov/study1",
            "https://who.int/guidelines-2024",
            "https://arxiv.org/abs/2501.12345",
        ]
        indices = []
        for url in urls:
            indices.append(cm.add(url, f"Title for {url}", "snippet"))

        # Check all indices are 1-indexed and sequential
        assert indices == [1, 2, 3]

        # Phase 4: format references
        refs = cm.format_references()
        for i in range(1, 4):
            assert f"[{i}]" in refs, f"Reference [{i}] missing from formatted output"

    def test_to_dict_returns_ordered_citations(self):
        cm = CitationManager()
        cm.add("https://c.com", "C", site_name="SiteC")
        cm.add("https://a.com", "A", site_name="SiteA")
        cm.add("https://b.com", "B", site_name="SiteB")

        d = cm.to_dict()
        assert d["count"] == 3
        citations = d["citations"]
        assert len(citations) == 3
        # Should be sorted by index
        assert citations[0]["index"] == 1
        assert citations[1]["index"] == 2
        assert citations[2]["index"] == 3

    def test_format_inline_refs_includes_all_indices(self):
        cm = CitationManager()
        cm.add("https://example.com/1", "Paper One")
        cm.add("https://example.com/2", "Paper Two")
        cm.add("https://example.com/3", "Paper Three")

        inline = cm.format_inline_refs()
        assert "[1]" in inline
        assert "[2]" in inline
        assert "[3]" in inline
        assert "Paper One" in inline
        assert "Paper Three" in inline

    def test_empty_cm_returns_empty_references(self):
        cm = CitationManager()
        assert cm.format_references() == ""
        assert cm.format_inline_refs() == ""
        assert cm.to_dict() == {"count": 0, "citations": []}

    def test_add_batch_preserves_order(self):
        cm = CitationManager()
        sources = [
            {"url": "https://a.com", "title": "A", "site_name": "SiteA"},
            {"url": "https://b.com", "title": "B", "site_name": "SiteB"},
            {"url": "https://c.com", "title": "C", "site_name": "SiteC"},
        ]
        indices = cm.add_batch(sources)
        assert indices == [1, 2, 3]


# ---------------------------------------------------------------------------
# extract_citation_indices tests
# ---------------------------------------------------------------------------

class TestExtractCitationIndices:
    """Verify [n] pattern extraction from section text."""

    def test_single_bracket_reference(self):
        text = "COVID-19 has significant neurological effects [1]."
        indices = extract_citation_indices(text)
        assert indices == [1]

    def test_multiple_single_references(self):
        text = "Studies show [1] that mRNA vaccines work [3], with boosters needed [5]."
        indices = extract_citation_indices(text)
        assert indices == [1, 3, 5]

    def test_comma_separated_references(self):
        text = "Multiple studies [1,2,3] confirm this finding."
        indices = extract_citation_indices(text)
        assert indices == [1, 2, 3]

    def test_range_reference(self):
        text = "Prior work [4-7] establishes the foundation."
        indices = extract_citation_indices(text)
        assert indices == [4, 5, 6, 7]

    def test_mixed_formats(self):
        text = "Research [1,3-5,8] supports this conclusion."
        indices = extract_citation_indices(text)
        assert indices == [1, 3, 4, 5, 8]

    def test_no_citations(self):
        text = "This paragraph has no citation markers."
        indices = extract_citation_indices(text)
        assert indices == []

    def test_ignore_non_citation_brackets(self):
        """Brackets with non-numeric content shouldn't be treated as citations."""
        text = "The python[1] programming language [note] is widely used."
        indices = extract_citation_indices(text)
        assert 1 in indices
        # "[note]" should not produce any index

    def test_chinese_text_with_citations(self):
        text = "多项研究表明[1,2]，COVID-19会导致神经系统症状[3]。"
        indices = extract_citation_indices(text)
        assert indices == [1, 2, 3]

    def test_duplicate_indices_deduplicated(self):
        text = "Source [1] is cited many times [1] in this section [1]."
        indices = extract_citation_indices(text)
        assert indices == [1]

    def test_large_range(self):
        text = "References [1-50] cover this topic."
        indices = extract_citation_indices(text)
        assert len(indices) == 50
        assert indices[0] == 1
        assert indices[-1] == 50


# ---------------------------------------------------------------------------
# Integration: CM consistency across simulated pipeline phases
# ---------------------------------------------------------------------------

class TestPipelineCitationFlow:
    """End-to-end citation consistency as seen by the pipeline."""

    def test_full_flow_phase1_to_phase4(self):
        """Simulate what happens in research_pipeline.run_research_pipeline()."""
        # Phase 1: ResearchEngine registers sources
        cm = CitationManager()
        cm.add("https://pubmed.ncbi.nlm.nih.gov/001", "COVID Neurological Study",
               snippet="30% of survivors...", source_type="academic", site_name="PubMed")
        cm.add("https://who.int/long-covid", "WHO Long COVID Guidelines",
               snippet="WHO recommends...", source_type="official", site_name="WHO")
        cm.add("https://arxiv.org/abs/2501", "Machine Learning for COVID",
               snippet="ML models show...", source_type="academic", site_name="arXiv")

        assert cm.count == 3

        # Phase 3: Section writing uses CM's inline_refs
        inline = cm.format_inline_refs()
        assert "[1]" in inline and "[2]" in inline and "[3]" in inline

        # Phase 4: assemble_report reads CM
        refs = cm.format_references()
        for i in range(1, 4):
            assert f"[{i}]" in refs

        # Verify URL → index mapping is stable
        assert cm.get_by_url("https://pubmed.ncbi.nlm.nih.gov/001").index == 1
        assert cm.get_by_url("https://who.int/long-covid").index == 2
        assert cm.get_by_index(3).url == "https://arxiv.org/abs/2501"

    def test_citation_indices_in_section_match_cm(self):
        """Section text with [n] should reference valid CM indices."""
        cm = CitationManager()
        cm.add("https://pubmed.ncbi.nlm.nih.gov/001", "Study A")
        cm.add("https://who.int/guide", "WHO Guide")
        cm.add("https://cdc.gov/data", "CDC Data")

        # Simulate section text generated by LLM
        section_text = (
            "Recent studies [1] have shown significant effects. "
            "WHO guidelines [2] recommend monitoring. "
            "CDC data [3] confirms these trends. "
            "Additionally, the foundational research [1,2] is well-cited."
        )

        # Extract indices and verify they exist in CM
        used = extract_citation_indices(section_text)
        assert used == [1, 2, 3]

        # Each referenced index must exist in CM
        for idx in used:
            citation = cm.get_by_index(idx)
            assert citation is not None, f"Citation [{idx}] not found in CM"
            assert citation.url, f"Citation [{idx}] has empty URL"
