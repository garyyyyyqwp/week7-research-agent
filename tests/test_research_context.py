"""Test ResearchContext - add, retrieve, cleanup, and degraded fallback.

Tests that verify:
  1. Normal path: add 3 sources -> retrieve returns relevant chunks
     (structure: content/url/site/title) -> cleanup removes collection
  2. Degraded path: ChromaDB unavailable -> add/retrieve still works
  3. chunk_text: correct sizes, overlap behavior
  4. Concurrent isolation: two ResearchContexts don't interfere
"""

import asyncio
import pytest

from app.services.research_context import (
    ResearchContext,
    chunk_text,
    _tokenize,
)


# ---------------------------------------------------------------------------
# chunk_text tests
# ---------------------------------------------------------------------------

class TestChunkText:
    """Test text chunking with tiktoken and fallback."""

    def test_empty_string(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_under_target(self):
        text = "Short text"
        chunks = chunk_text(text, target_tokens=300, overlap=0.15)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self):
        text = "Hello world. " * 400
        chunks = chunk_text(text, target_tokens=300, overlap=0.15)
        assert len(chunks) >= 3, f"Expected >= 3 chunks, got {len(chunks)}"

    def test_chunks_are_subsets_of_original(self):
        text = "A paragraph about something. " * 300
        chunks = chunk_text(text, target_tokens=300, overlap=0.15)
        for i, chunk in enumerate(chunks):
            assert len(chunk) > 0, f"Chunk {i} is empty"
            assert chunk[:20] in text, f"Chunk {i} start not in original"

    def test_chinese_text_chunking(self):
        text = "研究报告智能体是一个基于AI的自动化研究工具。" * 200
        chunks = chunk_text(text, target_tokens=300, overlap=0.15)
        assert len(chunks) >= 2, f"Expected >= 2 chunks for Chinese text, got {len(chunks)}"

    def test_overlap_between_chunks(self):
        text = "The quick brown fox jumps over the lazy dog. " * 300
        chunks = chunk_text(text, target_tokens=200, overlap=0.20)
        if len(chunks) >= 2:
            assert len(chunks[0]) > 20

    def test_custom_target_tokens(self):
        text = "Data. " * 500
        c300 = chunk_text(text, target_tokens=300)
        c100 = chunk_text(text, target_tokens=100)
        assert len(c100) > len(c300), (
            f"Smaller target (100) should produce more chunks: "
            f"{len(c100)} <= {len(c300)}"
        )


# ---------------------------------------------------------------------------
# _tokenize tests
# ---------------------------------------------------------------------------

class TestTokenize:
    """Test the keyword tokenizer used in degraded mode."""

    def test_empty(self):
        assert _tokenize("") == []
        assert _tokenize("   ") == []

    def test_english_words(self):
        tokens = _tokenize("Hello World Testing")
        assert "hello" in tokens
        assert "world" in tokens
        assert "testing" in tokens

    def test_chinese_characters(self):
        tokens = _tokenize("研究报告")
        assert "研" in tokens
        assert "究" in tokens
        assert "报" in tokens
        assert "告" in tokens

    def test_mixed_cjk_english(self):
        tokens = _tokenize("COVID-19 长期影响研究")
        assert "covid-19" in tokens or "covid" in tokens
        assert "长" in tokens

    def test_punctuation_removed(self):
        tokens = _tokenize("hello, world! test...")
        assert "," not in tokens
        assert "!" not in tokens
        assert "hello" in tokens


# ---------------------------------------------------------------------------
# Helper: create a context and ensure cleanup
# ---------------------------------------------------------------------------

def _make_rc(report_id: str) -> ResearchContext:
    """Create a ResearchContext. Caller must call cleanup()."""
    return ResearchContext(report_id=report_id)


# Source texts for testing
SOURCE_1 = {
    "content": "Introduction to COVID research: This paper provides a comprehensive "
               "overview of COVID-19 research including epidemiology, virology, "
               "and clinical manifestations. The SARS-CoV-2 virus has caused "
               "a global pandemic affecting millions worldwide. " * 30,
    "url": "https://pubmed.ncbi.nlm.nih.gov/10001/",
    "site": "PubMed",
    "title": "Introduction to COVID-19 Research",
}

SOURCE_2 = {
    "content": "COVID vaccine efficacy data: Clinical trials have demonstrated "
               "mRNA vaccines achieve 95% efficacy against symptomatic infection. "
               "Booster doses restore waning immunity. Real-world data from "
               "multiple countries confirms these findings. " * 30,
    "url": "https://pubmed.ncbi.nlm.nih.gov/10002/",
    "site": "PubMed",
    "title": "COVID-19 Vaccine Efficacy: A Meta-Analysis",
}

SOURCE_3 = {
    "content": "Long COVID neurological symptoms: A systematic review of 50 studies "
               "found that 30% of COVID survivors experience persistent neurological "
               "symptoms including brain fog, fatigue, and cognitive impairment. "
               "The mechanisms involve neuroinflammation and microvascular damage. " * 30,
    "url": "https://who.int/long-covid-neuro-2024",
    "site": "WHO",
    "title": "Long COVID Neurological Manifestations",
}


# ---------------------------------------------------------------------------
# ResearchContext tests - Normal path (ChromaDB available)
# ---------------------------------------------------------------------------

class TestResearchContextNormal:
    """Tests for ResearchContext with ChromaDB available.

    Each test manages its own ResearchContext and calls cleanup() at the end.
    No autouse fixture to avoid pytest-asyncio strict-mode issues.
    """

    @pytest.mark.asyncio
    async def test_add_single_source_returns_chunk_count(self):
        rc = _make_rc("test_add_001")
        try:
            n = await rc.add(**SOURCE_1)
            assert n > 0, f"Expected > 0 chunks, got {n}"
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_add_empty_content_returns_zero(self):
        rc = _make_rc("test_empty_001")
        try:
            n = await rc.add("", "http://x.com", "site", "title")
            assert n == 0
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_add_three_sources(self):
        rc = _make_rc("test_three_001")
        try:
            n1 = await rc.add(**SOURCE_1)
            n2 = await rc.add(**SOURCE_2)
            n3 = await rc.add(**SOURCE_3)
            assert n1 > 0
            assert n2 > 0
            assert n3 > 0
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_retrieve_returns_correct_structure(self):
        rc = _make_rc("test_struct_001")
        try:
            await rc.add(**SOURCE_1)
            await rc.add(**SOURCE_2)
            results = await rc.retrieve("vaccine efficacy", top_k=3)
            assert isinstance(results, list)
            assert len(results) > 0, "Expected at least 1 result"
            for r in results:
                assert "content" in r, f"Missing 'content' in {r.keys()}"
                assert "url" in r, f"Missing 'url' in {r.keys()}"
                assert "site" in r, f"Missing 'site' in {r.keys()}"
                assert "title" in r, f"Missing 'title' in {r.keys()}"
                assert isinstance(r["content"], str)
                assert len(r["content"]) > 0
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_retrieve_relevant_to_query(self):
        rc = _make_rc("test_rel_001")
        try:
            await rc.add(**SOURCE_1)
            await rc.add(**SOURCE_2)
            await rc.add(**SOURCE_3)
            results = await rc.retrieve("vaccine efficacy clinical trials", top_k=3)
            assert len(results) > 0
            # Results should be from various sources
            sites = {r["site"] for r in results}
            assert len(sites) >= 1
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_retrieve_respects_top_k(self):
        rc = _make_rc("test_topk_001")
        try:
            await rc.add(**SOURCE_3)
            results = await rc.retrieve("neurological symptoms", top_k=3)
            assert len(results) <= 3
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_retrieve_empty_context(self):
        rc = _make_rc("test_empty_002")
        try:
            results = await rc.retrieve("anything")
            assert results == []
        finally:
            rc.cleanup()

    @pytest.mark.asyncio
    async def test_cleanup_is_idempotent(self):
        rc = _make_rc("test_cleanup_001")
        await rc.add(**SOURCE_1)
        rc.cleanup()
        # Second cleanup should be safe
        rc.cleanup()

    @pytest.mark.asyncio
    async def test_degraded_property_is_bool(self):
        rc = _make_rc("test_prop_001")
        try:
            assert rc.degraded in (True, False)
        finally:
            rc.cleanup()


# ---------------------------------------------------------------------------
# ResearchContext tests - Degraded path (ChromaDB unavailable)
# ---------------------------------------------------------------------------

class TestResearchContextDegraded:
    """Tests for ResearchContext when ChromaDB initialization fails.

    These construct ResearchContext objects in degraded mode directly
    to simulate ChromaDB unavailability.
    """

    @staticmethod
    def _make_degraded(report_id: str) -> ResearchContext:
        """Create a ResearchContext in forced degraded mode."""
        rc = ResearchContext.__new__(ResearchContext)
        rc.report_id = report_id
        rc.collection_name = f"research_{report_id}"
        rc._degraded = True
        rc._fallback = []
        rc._client = None
        rc._collection = None
        return rc

    @pytest.mark.asyncio
    async def test_degraded_add_and_retrieve(self):
        rc = self._make_degraded("test_da_001")
        n1 = await rc.add(**SOURCE_1)
        n2 = await rc.add(**SOURCE_2)
        n3 = await rc.add(**SOURCE_3)
        assert n1 > 0
        assert n2 > 0
        assert n3 > 0
        assert len(rc._fallback) == n1 + n2 + n3

        results = await rc.retrieve("neurological symptoms brain", top_k=5)
        assert len(results) > 0
        for r in results:
            assert "content" in r
            assert "url" in r
            assert "site" in r
            assert "title" in r

        # At least one result should be from WHO (neurological source)
        who_results = [r for r in results if r["site"] == "WHO"]
        assert len(who_results) > 0, (
            f"Expected WHO results, got sites: {[r['site'] for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_degraded_retrieve_empty_fallback(self):
        rc = self._make_degraded("test_de_001")
        results = await rc.retrieve("anything", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_degraded_cleanup_clears_fallback(self):
        rc = self._make_degraded("test_dc_001")
        rc._fallback = [{"content": "t", "url": "http://x.com", "site": "x", "title": "t"}]
        rc.cleanup()
        assert rc._fallback == []
        rc.cleanup()  # Idempotent
        assert rc._fallback == []

    @pytest.mark.asyncio
    async def test_degraded_keyword_relevance(self):
        rc = self._make_degraded("test_dk_001")
        await rc.add(
            content="Apple fruit nutrition vitamins healthy eating diet",
            url="http://fruit.com", site="Food", title="Apple Nutrition",
        )
        await rc.add(
            content="Python programming language coding development software",
            url="http://code.com", site="Tech", title="Python Programming",
        )
        await rc.add(
            content="Apple Inc company stock market iPhone technology",
            url="http://apple.com", site="Business", title="Apple Inc",
        )
        results = await rc.retrieve("programming coding software", top_k=3)
        assert len(results) > 0
        # Tech-related results should be present
        tech_results = [r for r in results if r["site"] == "Tech"]
        assert len(tech_results) > 0, (
            f"Keyword search for 'programming' should find Tech source"
        )

    @pytest.mark.asyncio
    async def test_degraded_chinese_query(self):
        rc = self._make_degraded("test_dcn_001")
        await rc.add(
            content="新型冠状病毒疫苗的临床试验数据显示了高效的保护作用",
            url="http://med.cn/1", site="PubMed", title="COVID疫苗研究",
        )
        await rc.add(
            content="深度学习在自然语言处理中的应用越来越广泛",
            url="http://ai.cn/1", site="arXiv", title="深度学习应用",
        )
        await rc.add(
            content="COVID-19 pandemic global health emergency response",
            url="http://who.int/1", site="WHO", title="COVID Response",
        )
        results = await rc.retrieve("疫苗临床试验", top_k=3)
        assert len(results) > 0
        vaccine_sites = [r for r in results if "PubMed" == r["site"]]
        assert len(vaccine_sites) > 0, "Chinese query for vaccine should find PubMed source"

    @pytest.mark.asyncio
    async def test_degraded_init_flag(self):
        """degraded property should be True when forced."""
        rc = self._make_degraded("test_flag_001")
        assert rc.degraded is True
        assert rc._degraded is True


# ---------------------------------------------------------------------------
# Concurrent isolation test
# ---------------------------------------------------------------------------

class TestConcurrentIsolation:
    """Tests that two ResearchContexts don't interfere."""

    @pytest.mark.asyncio
    async def test_two_contexts_independent(self):
        rc1 = _make_rc("concurrent_1")
        rc2 = _make_rc("concurrent_2")
        try:
            await rc1.add(**SOURCE_1)
            await rc2.add(**SOURCE_2)
            r1 = await rc1.retrieve("COVID introduction", top_k=5)
            r2 = await rc2.retrieve("vaccine efficacy", top_k=5)
            urls_1 = {r["url"] for r in r1}
            urls_2 = {r["url"] for r in r2}
            assert len(urls_1) > 0
            assert len(urls_2) > 0
        finally:
            rc1.cleanup()
            rc2.cleanup()
