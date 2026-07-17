"""Research Context - temp vector index for a research report.

Purpose:
  - Solves Token overflow: Agent-fetched full text can't all go into prompts
  - Per-section RAG retrieval: only top_k chunks injected per section
  - ChromaDB collection namespaced by report_id for concurrent isolation

Degradation (PROJECT_PLAN.md section 3.3):
  If ChromaDB init fails (missing deps / low memory), auto-falls-back to
  in-memory list + keyword overlap scoring. Pipeline never crashes.

Chunking (PROJECT_PLAN.md section 3.2):
  - tiktoken exact count, ~300 tokens/chunk
  - 15% overlap (~45 tokens), prevents sentence splitting
  - Falls back to ~1200 chars/chunk for Chinese (no tiktoken)

Cleanup (PROJECT_PLAN.md section 4.4):
  pipeline finally always calls cleanup(); FIFO eviction as safety net.
"""

import asyncio
import hashlib
import logging
from typing import Any

from app.services.embeddings import embed_batch
from app.utils.config import CHUNK_TARGET_TOKENS, CHUNK_OVERLAP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token-based text chunking
# ---------------------------------------------------------------------------

# tiktoken encoder 进程级单例：cl100k_base 的 BPE 文件首次使用会联网下载 (~1.7MB)，
# Render 冷启动后每次 chunk_text 重复 get_encoding 会反复触发下载/磁盘读
_TIKTOKEN_ENCODER = None
_TIKTOKEN_TRIED = False


def _get_encoder():
    """Return a cached tiktoken encoder, or None if unavailable."""
    global _TIKTOKEN_ENCODER, _TIKTOKEN_TRIED
    if not _TIKTOKEN_TRIED:
        _TIKTOKEN_TRIED = True
        try:
            import tiktoken
            _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            logger.debug("tiktoken not available, using char-based chunking")
    return _TIKTOKEN_ENCODER


def _token_count(text: str, encoder=None) -> int:
    """Count tokens using tiktoken, or fall back to char-based estimate."""
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def chunk_text(
    content: str,
    target_tokens: int = CHUNK_TARGET_TOKENS,
    overlap: float = CHUNK_OVERLAP,
) -> list[str]:
    """Split text into ~target_tokens chunks with overlap.

    Uses tiktoken (cl100k_base) for accurate counting when available.
    Falls back to ~4 chars/token estimate for mixed CJK+English.

    Args:
        content: Full text to split.
        target_tokens: Target token count per chunk (default 300).
        overlap: Overlap ratio between consecutive chunks (default 0.15).

    Returns:
        List of text chunks.
    """
    if not content or not content.strip():
        return []

    encoder = _get_encoder()

    total_tokens = _token_count(content, encoder)
    chunk_size_tokens = target_tokens
    overlap_tokens = int(chunk_size_tokens * overlap)

    if total_tokens <= chunk_size_tokens:
        return [content.strip()]

    chunks: list[str] = []
    chars_per_token_est = len(content) / total_tokens

    pos = 0
    while pos < len(content):
        chunk_chars = int(chunk_size_tokens * chars_per_token_est)
        end = min(pos + chunk_chars, len(content))
        raw = content[pos:end].strip()
        if raw:
            chunks.append(raw)

        step_tokens = max(1, chunk_size_tokens - overlap_tokens)
        step_chars = int(step_tokens * chars_per_token_est)
        pos += step_chars
        if step_chars < 1:
            pos = end

    return chunks


# ---------------------------------------------------------------------------
# ResearchContext
# ---------------------------------------------------------------------------


class ResearchContext:
    """Temporary vector index for a single research report.

    Each report_id gets its own ChromaDB EphemeralClient collection (pure memory).
    Collection is deleted on cleanup(), never persisted to disk.

    Degraded mode: if ChromaDB init fails, automatically switches to
    in-memory list + keyword-overlap retrieval.

    Usage:
        rc = ResearchContext(report_id="abc123")
        n = await rc.add(full_text, url="...", site="PubMed", title="...")
        chunks = await rc.retrieve("Section title", top_k=8)
        rc.cleanup()
    """

    def __init__(self, report_id: str):
        self.report_id = report_id
        self.collection_name = f"research_{report_id}"
        self._degraded = False
        self._fallback: list[dict[str, Any]] = []
        self._client = None
        self._collection = None

        try:
            import chromadb
            self._client = chromadb.EphemeralClient()
            self._collection = self._client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "ResearchContext[%s]: ChromaDB collection '%s' created",
                report_id, self.collection_name,
            )
        except Exception as e:
            logger.warning(
                "ResearchContext[%s]: ChromaDB init failed (%s), "
                "falling back to in-memory keyword search",
                report_id, e,
            )
            self._degraded = True
            self._fallback = []

    @property
    def degraded(self) -> bool:
        """Whether the context is operating in degraded (no-ChromaDB) mode."""
        return self._degraded

    def _migrate_collection_to_fallback(self) -> None:
        """Copy chunks already stored in ChromaDB into the fallback list.

        Must run BEFORE flipping to degraded mid-run: degraded retrieve()
        only reads _fallback, so without migration every chunk indexed
        before the failure would be silently stranded and Phase 3 would
        write sections from a fraction of the collected sources.
        """
        if self._collection is None:
            return
        try:
            data = self._collection.get(include=["documents", "metadatas"])
            docs = data.get("documents") or []
            metas = data.get("metadatas") or []
            existing = {
                (item["url"], item.get("chunk_idx", -1)) for item in self._fallback
            }
            migrated = 0
            for doc, m in zip(docs, metas):
                m = m or {}
                key = (m.get("url", ""), m.get("chunk_idx", -1))
                if key in existing:
                    continue
                self._fallback.append({
                    "content": doc,
                    "url": m.get("url", ""),
                    "site": m.get("site", ""),
                    "title": m.get("title", ""),
                    "chunk_idx": m.get("chunk_idx", -1),
                })
                existing.add(key)
                migrated += 1
            if migrated:
                logger.info(
                    "ResearchContext[%s]: migrated %d chunks from ChromaDB "
                    "to fallback before degrading",
                    self.report_id, migrated,
                )
        except Exception as e:
            logger.warning(
                "ResearchContext[%s]: collection migration failed: %s",
                self.report_id, e,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(
        self,
        content: str,
        url: str,
        site: str,
        title: str,
    ) -> int:
        """Add a source's full text to the index.

        Chunks the content (~300 tokens/chunk, 15% overlap), computes embeddings,
        stores in ChromaDB (or fallback list). Each chunk's metadata carries
        {url, site, title, chunk_idx}.

        Args:
            content: Full text of the source.
            url: Source URL.
            site: Site name (e.g. "PubMed", "WHO").
            title: Article title.

        Returns:
            Number of chunks stored.
        """
        if not content or not content.strip():
            return 0

        chunks = chunk_text(content)
        if not chunks:
            return 0

        if self._degraded:
            for i, chunk in enumerate(chunks):
                self._fallback.append({
                    "content": chunk,
                    "url": url,
                    "site": site,
                    "title": title,
                    "chunk_idx": i,
                })
            logger.debug(
                "ResearchContext[%s]: degraded add - %d chunks for '%s'",
                self.report_id, len(chunks), title[:60],
            )
            return len(chunks)

        # Normal path: embed + ChromaDB
        try:
            embeds = await embed_batch(chunks)
        except Exception as e:
            logger.error(
                "ResearchContext[%s]: embedding failed for '%s', "
                "switching to degraded mode: %s",
                self.report_id, title[:60], e,
            )
            self._migrate_collection_to_fallback()
            self._degraded = True
            for i, chunk in enumerate(chunks):
                self._fallback.append({
                    "content": chunk,
                    "url": url,
                    "site": site,
                    "title": title,
                    "chunk_idx": i,
                })
            return len(chunks)

        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        ids = [f"{url_hash}_{i}" for i in range(len(chunks))]
        metadatas = [
            {"url": url, "site": site, "title": title, "chunk_idx": i}
            for i in range(len(chunks))
        ]

        try:
            self._collection.add(
                ids=ids,
                documents=chunks,
                embeddings=embeds,
                metadatas=metadatas,
            )
            logger.debug(
                "ResearchContext[%s]: added %d chunks from '%s' (%s)",
                self.report_id, len(chunks), title[:60], site,
            )
        except Exception as e:
            logger.error(
                "ResearchContext[%s]: ChromaDB add failed, "
                "switching to degraded mode: %s",
                self.report_id, e,
            )
            self._migrate_collection_to_fallback()
            self._degraded = True
            for i, chunk in enumerate(chunks):
                self._fallback.append({
                    "content": chunk,
                    "url": url,
                    "site": site,
                    "title": title,
                    "chunk_idx": i,
                })

        return len(chunks)

    async def retrieve(
        self,
        section_title: str,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Retrieve the most relevant chunks for a section.

        Embeds section_title, queries ChromaDB for top_k most similar chunks.
        Falls back to keyword-overlap scoring in degraded mode.

        Args:
            section_title: The section title or description to query against.
            top_k: Max number of chunks to return.

        Returns:
            List of dicts: [{content, url, site, title}, ...]
        """
        if self._degraded:
            return self._retrieve_degraded(section_title, top_k)

        try:
            q_embed = await embed_batch([section_title])
            if not q_embed:
                return []

            res = self._collection.query(
                query_embeddings=[q_embed[0]],
                n_results=top_k,
            )

            documents = res.get("documents", [[]])[0]
            metadatas = res.get("metadatas", [[]])[0]

            return [
                {
                    "content": doc,
                    "url": m.get("url", ""),
                    "site": m.get("site", ""),
                    "title": m.get("title", ""),
                }
                for doc, m in zip(documents, metadatas)
            ]

        except Exception as e:
            logger.error(
                "ResearchContext[%s]: ChromaDB retrieve failed, "
                "degrading: %s",
                self.report_id, e,
            )
            # 关键：正常模式下 chunks 都在 ChromaDB 里，_fallback 是空的。
            # 必须先迁移再降级检索，否则本节及后续节全部拿到 0 条材料。
            self._migrate_collection_to_fallback()
            self._degraded = True
            return self._retrieve_degraded(section_title, top_k)

    def cleanup(self):
        """Delete the temporary collection and free resources.

        Idempotent - safe to call multiple times. Exceptions are swallowed
        because cleanup must never crash the pipeline.

        Note: the collection is deleted whenever a client exists, regardless
        of the degraded flag — mid-run degradation would otherwise leak the
        already-created collection.
        """
        self._fallback.clear()
        if self._client is not None:
            try:
                self._client.delete_collection(self.collection_name)
                logger.info(
                    "ResearchContext[%s]: collection '%s' deleted",
                    self.report_id, self.collection_name,
                )
            except Exception as e:
                logger.warning(
                    "ResearchContext[%s]: cleanup failed (non-fatal): %s",
                    self.report_id, e,
                )
            finally:
                self._collection = None
                self._client = None

    # ------------------------------------------------------------------
    # Degraded mode: keyword-overlap retrieval
    # ------------------------------------------------------------------

    def _retrieve_degraded(
        self,
        query: str,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Keyword-overlap retrieval for degraded mode.

        Tokenizes query and each fallback chunk, scores by overlapping token count.
        Simple but effective without embeddings.
        """
        if not self._fallback:
            return []

        query_tokens = set(_tokenize(query))
        if not query_tokens:
            # No query tokens - return first top_k items
            results = []
            seen = set()
            for item in self._fallback:
                key = (item["url"], item.get("chunk_idx", -1))
                if key not in seen:
                    results.append({
                        "content": item["content"],
                        "url": item["url"],
                        "site": item["site"],
                        "title": item["title"],
                    })
                    seen.add(key)
                if len(results) >= top_k:
                    break
            return results

        scored: list[tuple[int, dict]] = []
        for item in self._fallback:
            content_tokens = _tokenize(item["content"])
            overlap = len(query_tokens & set(content_tokens))
            if overlap > 0:
                scored.append((overlap, item))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        seen = set()
        for _, item in scored:
            key = (item["url"], item.get("chunk_idx", -1))
            if key not in seen:
                results.append({
                    "content": item["content"],
                    "url": item["url"],
                    "site": item["site"],
                    "title": item["title"],
                })
                seen.add(key)
            if len(results) >= top_k:
                break

        # Pad with unseen items if keyword match didn't fill top_k
        if len(results) < top_k:
            for item in self._fallback:
                key = (item["url"], item.get("chunk_idx", -1))
                if key not in seen:
                    results.append({
                        "content": item["content"],
                        "url": item["url"],
                        "site": item["site"],
                        "title": item["title"],
                    })
                    seen.add(key)
                if len(results) >= top_k:
                    break

        logger.debug(
            "ResearchContext[%s]: degraded retrieve '%s' -> %d results (from %d items)",
            self.report_id, query[:60], len(results), len(self._fallback),
        )
        return results


# ---------------------------------------------------------------------------
# Tokenization helper for degraded mode
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Simple tokenizer for CJK + English mixed text.

    Splits on whitespace and punctuation. CJK segments get char-level tokens;
    alphabetic segments stay as word tokens.
    """
    import re

    text = text.lower().strip()
    if not text:
        return []

    words: list[str] = []
    # Split on common punctuation and whitespace
    parts = re.split(r"[\s\.,;:!?()\[\]{}/\\|@#$%^&*+=~`\"'_\-]+", text)
    for segment in parts:
        segment = segment.strip()
        if not segment:
            continue
        # CJK characters: tokenize individually
        cjk = [c for c in segment if '一' <= c <= '鿿']
        if len(cjk) >= len(segment) * 0.5:
            words.extend(cjk)
        else:
            words.append(segment)

    return [w for w in words if len(w) >= 1]
