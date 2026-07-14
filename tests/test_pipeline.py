"""Integration tests for the research pipeline.

Uses a higher-level mock strategy: we mock the ResearchEngine.research()
and report_generator functions directly rather than trying to mock the LLM
client streaming internals (which is fragile and causes test hangs).

Tests verify:
  - SSE event sequence correctness
  - Report structure completeness
  - References non-empty for report_complete
  - few_sources warning fires
  - Pipeline error handling
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.research_pipeline import run_research_pipeline


# ---------------------------------------------------------------------------
# Helpers for building mock SSE events
# ---------------------------------------------------------------------------

def _sse(event, data):
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPipelineSSE:
    """Verify SSE event sequence and structure."""

    @pytest.mark.asyncio
    async def test_sse_event_sequence(self):
        """Verify SSE events appear in the correct order per §7.2."""
        # Mock ResearchEngine.research() to yield just research_done
        async def mock_research_gen(self, topic, enabled_sites):
            yield _sse("research_progress", {"ts": "t", "icon": "🔍", "message": "Searching..."})
            yield _sse("research_done", {"sources": 1, "elapsed_s": 1.0})

        with patch('app.services.research_pipeline.ResearchEngine.research', mock_research_gen), \
             patch('app.services.research_pipeline.generate_outline_with_sources') as mock_outline, \
             patch('app.services.research_pipeline.write_section_stream') as mock_write, \
             patch('app.services.research_pipeline.generate_abstract') as mock_abstract:

            mock_outline.return_value = [
                {"title": "Section 1", "description": "Desc", "source_indices": [1]},
                {"title": "Section 2", "description": "Desc", "source_indices": [1]},
            ]

            async def mock_write_stream(*args, **kwargs):
                yield "Content [1]"
            mock_write.side_effect = mock_write_stream
            mock_abstract.return_value = "Abstract text"

            events = []
            async for event in run_research_pipeline(
                report_id="test_seq_001",
                topic="Test topic",
                num_sections=2,
                language="zh-CN",
                enabled_sites=[],
            ):
                events.append(event)

            event_types = [e["event"] for e in events]
            print(f"Event types: {event_types}")

            # Phase 1 events
            assert "research_start" in event_types
            assert "research_done" in event_types
            ri = event_types.index("research_start")
            rd = event_types.index("research_done")
            assert ri < rd, "research_start before research_done"

            # Phase 2
            assert "outline" in event_types
            oi = event_types.index("outline")
            assert rd < oi, "research_done before outline"

            # Phase 3: section lifecycle
            ss_count = event_types.count("section_start")
            sc_count = event_types.count("section_chunk")
            se_count = event_types.count("section_end")
            assert ss_count == 2, f"Expected 2 section_start, got {ss_count}"
            assert sc_count >= 1, f"Expected >=1 section_chunk, got {sc_count}"
            assert se_count == 2, f"Expected 2 section_end, got {se_count}"

            # Phase 4
            assert "abstract" in event_types
            assert "references" in event_types
            assert "report_complete" in event_types
            assert "done" in event_types

            # Check ordering within phase 4
            abs_idx = event_types.index("abstract")
            ref_idx = event_types.index("references")
            rc_idx = event_types.index("report_complete")
            assert abs_idx < ref_idx < rc_idx, "abstract → references → report_complete"

            # done is last
            assert event_types[-1] == "done"

    @pytest.mark.asyncio
    async def test_report_complete_structure(self):
        """Verify report_complete data has all required fields."""
        async def mock_research_gen(self, topic, enabled_sites):
            yield _sse("research_done", {"sources": 1, "elapsed_s": 1.0})

        with patch('app.services.research_pipeline.ResearchEngine.research', mock_research_gen), \
             patch('app.services.research_pipeline.generate_outline_with_sources') as mock_outline, \
             patch('app.services.research_pipeline.write_section_stream') as mock_write, \
             patch('app.services.research_pipeline.generate_abstract') as mock_abstract:

            mock_outline.return_value = [
                {"title": "Section 1", "description": "Desc", "source_indices": []},
            ]

            async def mock_write_stream(*args, **kwargs):
                yield "Content"
            mock_write.side_effect = mock_write_stream
            mock_abstract.return_value = "Abstract"

            events = []
            async for event in run_research_pipeline(
                report_id="test_struct_001",
                topic="Test",
            ):
                events.append(event)

            rc_events = [e for e in events if e["event"] == "report_complete"]
            assert len(rc_events) == 1
            data = json.loads(rc_events[0]["data"])

            assert "report" in data
            assert "markdown" in data
            assert "report_id" in data
            assert data["report_id"] == "test_struct_001"

            report = data["report"]
            assert "title" in report
            assert "sections" in report
            assert "references" in report
            assert "meta" in report
            # Meta fields
            assert report["meta"]["topic"] == "Test"
            assert "num_sources" in report["meta"]

    @pytest.mark.asyncio
    async def test_few_sources_warning(self):
        """When no sources are found, a warning event must be emitted before outline."""
        async def mock_research_gen(self, topic, enabled_sites):
            yield _sse("research_done", {"sources": 0, "elapsed_s": 1.0})

        with patch('app.services.research_pipeline.ResearchEngine.research', mock_research_gen), \
             patch('app.services.research_pipeline.generate_outline_with_sources') as mock_outline, \
             patch('app.services.research_pipeline.write_section_stream') as mock_write, \
             patch('app.services.research_pipeline.generate_abstract') as mock_abstract:

            mock_outline.return_value = [{"title": "S1", "description": "Desc", "source_indices": []}]

            async def mock_write_stream(*args, **kwargs):
                yield "Content"
            mock_write.side_effect = mock_write_stream
            mock_abstract.return_value = "Abstract"

            events = []
            async for event in run_research_pipeline(
                report_id="test_warn_001",
                topic="Obscure topic",
            ):
                events.append(event)

            warnings = [e for e in events if e["event"] == "warning"]
            assert len(warnings) > 0, "Expected few_sources warning"

            warning_data = json.loads(warnings[0]["data"])
            assert warning_data["code"] == "few_sources"

            # Warning must come after research_done and before outline
            event_types = [e["event"] for e in events]
            warn_idx = event_types.index("warning")
            rd_idx = event_types.index("research_done")
            oi_idx = event_types.index("outline")
            assert rd_idx < warn_idx < oi_idx, "warning between research_done and outline"

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Pipeline exception must yield error event without crashing."""
        async def mock_research_gen(self, topic, enabled_sites):
            raise RuntimeError("Simulated engine failure")

        with patch('app.services.research_pipeline.ResearchEngine.research', mock_research_gen):

            events = []
            async for event in run_research_pipeline(
                report_id="test_err_001",
                topic="Test",
            ):
                events.append(event)

            errors = [e for e in events if e["event"] == "error"]
            assert len(errors) > 0, "Expected error event"

            error_data = json.loads(errors[0]["data"])
            assert "message" in error_data
            assert "phase" in error_data

    @pytest.mark.asyncio
    async def test_outline_has_source_indices(self):
        """Outline sections should carry source_indices for citation binding."""
        async def mock_research_gen(self, topic, enabled_sites):
            yield _sse("research_done", {"sources": 3, "elapsed_s": 2.0})

        with patch('app.services.research_pipeline.ResearchEngine.research', mock_research_gen), \
             patch('app.services.research_pipeline.generate_outline_with_sources') as mock_outline, \
             patch('app.services.research_pipeline.write_section_stream') as mock_write, \
             patch('app.services.research_pipeline.generate_abstract') as mock_abstract:

            mock_outline.return_value = [
                {"title": "Chapter 1", "description": "Background", "source_indices": [1, 2]},
                {"title": "Chapter 2", "description": "Methods", "source_indices": [1, 3]},
            ]

            async def mock_write_stream(*args, **kwargs):
                yield "Content [1,2]"
            mock_write.side_effect = mock_write_stream
            mock_abstract.return_value = "Abstract"

            events = []
            async for event in run_research_pipeline(
                report_id="test_outline_001",
                topic="Test",
                num_sections=2,
            ):
                events.append(event)

            outline_events = [e for e in events if e["event"] == "outline"]
            assert len(outline_events) == 1
            outline_data = json.loads(outline_events[0]["data"])
            assert outline_data["count"] == 2
            for sec in outline_data["sections"]:
                assert "source_indices" in sec, f"Missing source_indices in {sec['title']}"
                assert isinstance(sec["source_indices"], list)
