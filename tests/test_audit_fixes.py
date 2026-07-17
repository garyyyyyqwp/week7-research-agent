"""审计修复回归测试 (2026-07-18)。

覆盖本轮审计确认并修复的缺陷，防止回退：
  1. refine 前缀剥离不再误删以"优化"开头的正文
  2. cleanup() 真正删除 ChromaDB collection（含降级后）
  3. 中途嵌入失败 → 已入库 chunks 迁移到 fallback（不再丢弃前期资料）
  4. retrieve 阶段嵌入失败 → 降级检索仍能取到已入库材料
  5. SSRF 服务层守卫 validate_public_url
  6. PDF 渲染资源白名单 _safe_pdf_url_fetcher
  7. CitationManager 注册入口清洗 HTML / 参考文献条目空行分隔
"""

import pytest

from app.routers.report import _strip_refine_prefix, _safe_pdf_url_fetcher
from app.services.citation_manager import CitationManager, _clean_text
from app.services.content_fetcher import fetch_url, validate_public_url
from app.services.research_context import ResearchContext


def _fake_embed(dim: int = 8):
    """确定性的假嵌入（按 chunk 文本哈希），保证测试离线可跑。"""
    async def _embed(texts):
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> (i * 4)) % 97) / 97.0 for i in range(dim)])
        return out
    return _embed


# ---------------------------------------------------------------------------
# 1. refine 前缀剥离
# ---------------------------------------------------------------------------

class TestStripRefinePrefix:
    def test_strips_explanation_prefix(self):
        assert _strip_refine_prefix("优化后：这是结果。") == "这是结果。"
        assert _strip_refine_prefix("优化结果: 内容") == "内容"
        assert _strip_refine_prefix("优化：\n多行内容\n第二行") == "多行内容\n第二行"

    def test_preserves_content_starting_with_youhua(self):
        """以"优化"开头的合法正文绝不能被删首行（此前的 bug）。"""
        text = "优化营商环境是当前的重点任务。\n具体而言，应当加强制度建设。"
        assert _strip_refine_prefix(text) == text

    def test_preserves_single_line_youhua_sentence(self):
        text = "优化是持续的过程。"
        assert _strip_refine_prefix(text) == text

    def test_plain_text_untouched(self):
        assert _strip_refine_prefix("普通润色结果。") == "普通润色结果。"


# ---------------------------------------------------------------------------
# 2-4. ResearchContext 降级链路
# ---------------------------------------------------------------------------

SRC_A = dict(content="COVID neurological effects research findings. " * 30,
             url="http://a.example.com/1", site="PubMed", title="Source A")
SRC_B = dict(content="Vaccine efficacy clinical trial data analysis. " * 30,
             url="http://b.example.com/2", site="WHO", title="Source B")


class TestCleanupDeletesCollection:
    @pytest.mark.asyncio
    async def test_cleanup_removes_collection(self, monkeypatch):
        """DoD 断言：cleanup 后 collection 不存在（此前测试从未验证删除）。"""
        monkeypatch.setattr(
            "app.services.research_context.embed_batch", _fake_embed())
        rc = ResearchContext("audit_cleanup_1")
        if rc.degraded:
            pytest.skip("ChromaDB unavailable in this environment")
        client = rc._client
        await rc.add(**SRC_A)
        names = [c.name for c in client.list_collections()]
        assert rc.collection_name in names
        rc.cleanup()
        names_after = [c.name for c in client.list_collections()]
        assert rc.collection_name not in names_after


class TestMidRunDegradationMigration:
    @pytest.mark.asyncio
    async def test_prior_chunks_survive_embedding_failure(self, monkeypatch):
        """中途嵌入失败：来源 A 的 chunks 必须迁移进 fallback，不能被丢弃。"""
        good = _fake_embed()

        monkeypatch.setattr("app.services.research_context.embed_batch", good)
        rc = ResearchContext("audit_migrate_1")
        if rc.degraded:
            pytest.skip("ChromaDB unavailable in this environment")
        client = rc._client
        n_a = await rc.add(**SRC_A)
        assert n_a > 0 and not rc.degraded

        async def _fail(texts):
            raise RuntimeError("embedding API 429")

        monkeypatch.setattr("app.services.research_context.embed_batch", _fail)
        n_b = await rc.add(**SRC_B)
        assert n_b > 0
        assert rc.degraded

        # A 的 chunks 已迁移（不只是 B 的）
        urls = {item["url"] for item in rc._fallback}
        assert SRC_A["url"] in urls, "来源A的chunks被丢弃 — 迁移逻辑失效"
        assert SRC_B["url"] in urls

        # 降级后的检索仍能取到 A 的材料
        results = await rc.retrieve("COVID neurological research", top_k=5)
        assert any(r["url"] == SRC_A["url"] for r in results)

        # 降级后 cleanup 仍必须删除已创建的 collection（不泄漏）
        rc.cleanup()
        names_after = [c.name for c in client.list_collections()]
        assert rc.collection_name not in names_after


class TestRetrieveFailureFallback:
    @pytest.mark.asyncio
    async def test_retrieve_falls_back_to_stored_chunks(self, monkeypatch):
        """Phase 3 检索时嵌入失败：不能返回空材料（此前每节拿 0 条）。"""
        monkeypatch.setattr(
            "app.services.research_context.embed_batch", _fake_embed())
        rc = ResearchContext("audit_retrieve_1")
        if rc.degraded:
            pytest.skip("ChromaDB unavailable in this environment")
        await rc.add(**SRC_A)
        assert not rc.degraded

        async def _fail(texts):
            raise RuntimeError("embedding API down")

        monkeypatch.setattr("app.services.research_context.embed_batch", _fail)
        results = await rc.retrieve("neurological effects", top_k=5)
        assert results, "检索失败时应迁移已入库chunks做关键词检索，而非返回空"
        assert rc.degraded
        rc.cleanup()


# ---------------------------------------------------------------------------
# 5. SSRF 服务层守卫
# ---------------------------------------------------------------------------

class TestValidatePublicUrl:
    @pytest.mark.parametrize("bad", [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
    ])
    def test_blocked(self, bad):
        with pytest.raises(ValueError):
            validate_public_url(bad)

    @pytest.mark.parametrize("good", [
        "https://pubmed.ncbi.nlm.nih.gov/12345/",
        "http://example.com/article",
    ])
    def test_allowed(self, good):
        assert validate_public_url(good) == good

    @pytest.mark.asyncio
    async def test_fetch_url_blocks_without_network(self):
        """fetch_url 服务层入口直接拦截（Agent 工具链绕过路由校验的路径）。"""
        result = await fetch_url("http://169.254.169.254/latest/meta-data/")
        assert result["strategy"] == "blocked"
        assert result["content"] == ""
        assert "拦截" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 6. PDF 渲染资源白名单
# ---------------------------------------------------------------------------

class TestPdfUrlFetcher:
    @pytest.mark.parametrize("bad", [
        "http://169.254.169.254/latest/meta-data/",
        "https://evil.example.com/x.png",
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
    ])
    def test_blocks_non_font_resources(self, bad):
        with pytest.raises(ValueError):
            _safe_pdf_url_fetcher(bad)


# ---------------------------------------------------------------------------
# 7. Citation 清洗与参考文献格式
# ---------------------------------------------------------------------------

class TestCitationSanitization:
    def test_clean_text_strips_tags(self):
        assert "img" not in _clean_text('恶意<img src=x onerror=alert(1)>标题')
        assert _clean_text('恶意<img src=x onerror=alert(1)>标题') == "恶意标题"

    def test_clean_text_keeps_math_lt(self):
        # "p<0.05" 没有闭合 > ，不能被误删
        assert _clean_text("显著性 p<0.05 的结果") == "显著性 p<0.05 的结果"

    def test_add_sanitizes_title(self):
        cm = CitationManager()
        idx = cm.add("http://x.com", '<script>bad</script>Real Title', "s<b>n</b>ippet")
        c = cm.get_by_index(idx)
        assert "<script>" not in c.title
        assert "Real Title" in c.title
        assert "<b>" not in c.snippet

    def test_references_blank_line_separated(self):
        cm = CitationManager()
        cm.add("http://a.com", "Title A", site_name="PubMed")
        cm.add("http://b.com", "Title B", site_name="WHO")
        refs = cm.format_references()
        # 条目间必须空行分隔，否则 markdown 渲染成一整段
        assert "\n\n[2]" in refs.replace("\n\n[1]", "") or "\n\n" in refs
        entries = [b for b in refs.split("\n\n") if b.strip().startswith("[")]
        assert len(entries) == 2
        # URL 行不能有 4 空格缩进（markdown 代码块语法）
        for line in refs.split("\n"):
            assert not line.startswith("    http")
