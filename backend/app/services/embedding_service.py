"""Chunk a document's text and store embeddings for search.

Providers (EMBEDDING_PROVIDER):
- "hash": a built-in hashing embedder. NOT a semantic model: it only captures shared words, so it is
  fine for demos and tests, not for real search quality.
- "none": skip this stage.
A real embedding model plugs in by implementing EmbeddingClient and adding it to get_embedding_client().
Anthropic offers no embedding endpoint; use a provider such as Voyage AI or an open-source model.
"""
import hashlib
import logging
import math
import re
import uuid
from abc import ABC, abstractmethod

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import DocumentPage, Embedding
from app.services.pipeline import stage

log = logging.getLogger(__name__)
_WORD = re.compile(r"\w+", re.UNICODE)


class EmbeddingClient(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector of length EMBEDDING_DIMENSION per text."""


class HashEmbedder(EmbeddingClient):
    """Feature hashing of words into `dim` buckets, signed, L2-normalised."""

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in _WORD.findall(t.casefold()):
                h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big")
                v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def get_embedding_client() -> EmbeddingClient | None:
    if settings.embedding_provider == "none":
        return None
    if settings.embedding_provider == "hash":
        return HashEmbedder(settings.embedding_dimension)
    raise ValueError(f"Unknown EMBEDDING_PROVIDER '{settings.embedding_provider}'")


def chunk_size_chars() -> tuple[int, int]:
    """(size, overlap) in characters for ~500 tokens with 10% overlap, using chars_per_token."""
    size = max(50, round(settings.embedding_chunk_tokens * settings.chars_per_token))
    return size, round(size * settings.embedding_chunk_overlap_ratio)


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split on whitespace into ~500-token chunks (~2000 characters) that overlap by 10%.

    Tokens are estimated from characters, not counted with the embedding model's tokenizer, so a chunk
    is "about" 500 tokens (Bengali and other scripts use more tokens per character).
    """
    default_size, default_overlap = chunk_size_chars()
    size = size or default_size
    overlap = default_overlap if overlap is None else overlap
    text = re.sub(r"[ \t]+", " ", text).strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # cut at whitespace, not mid-word
            cut = text.rfind(" ", start + size // 2, end)
            end = cut if cut > start else end
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        nxt = text.find(" ", start, end)  # start the next chunk at a word boundary, not mid-word
        if nxt != -1:
            start = nxt + 1
    return chunks


def embed_document(session: Session, document_id: uuid.UUID, client: EmbeddingClient | None = None) -> int:
    """Replace the document's embeddings. Never raises: a failure is logged in processing_history and
    leaves the document's status alone (search is secondary to extraction). Returns the chunk count."""
    try:
        client = client or get_embedding_client()
        if client is None:
            return 0
        with stage(session, document_id, "embed") as d:
            session.execute(delete(Embedding).where(Embedding.document_id == document_id))  # idempotent
            total, pending = 0, []  # pending: (page_number, chunk_text)

            def flush():
                nonlocal total
                if not pending:
                    return
                for (page_no, text), vec in zip(pending, client.embed([t for _, t in pending])):
                    session.add(Embedding(document_id=document_id, chunk_index=total, page_number=page_no,
                                          chunk_text=text, embedding=vec))
                    total += 1
                session.commit()
                pending.clear()

            last_page = 0
            while True:  # pages in small groups: a 1000-page PDF is never fully in memory
                rows = session.execute(
                    select(DocumentPage.page_number, DocumentPage.extracted_text)
                    .where(DocumentPage.document_id == document_id, DocumentPage.page_number > last_page)
                    .order_by(DocumentPage.page_number).limit(25)).all()
                if not rows:
                    break
                for page_number, text in rows:
                    pending.extend((page_number, c) for c in chunk_text(text or ""))
                    last_page = page_number
                    if len(pending) >= settings.embedding_batch_size:
                        flush()
            flush()
            d.update(chunks=total, provider=settings.embedding_provider)
        return total
    except Exception:
        log.exception("Embedding failed for %s", document_id)
        session.rollback()
        return 0
