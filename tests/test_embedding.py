import hashlib
import math
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document, DocumentPage, DocumentStatus, Embedding, ExtractionMethod
from app.services import embedding_service as es


def test_chunking_overlaps_and_cuts_on_spaces():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = es.chunk_text(text, size=200, overlap=40)
    assert len(chunks) > 5 and all(len(c) <= 200 for c in chunks)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
    assert chunks[0].split()[-1] in chunks[1]  # overlap: the end of one chunk starts the next
    assert " ".join(chunks).count("word399") >= 1 and "word399" in chunks[-1]
    assert not any(c.split()[0].startswith("ord") for c in chunks)  # never cut mid-word


def test_chunking_edge_cases():
    assert es.chunk_text("") == [] and es.chunk_text("   \n ") == []
    assert es.chunk_text("short text", size=100, overlap=10) == ["short text"]
    assert es.chunk_text("x" * 500, size=100, overlap=10)  # a single huge word still terminates


def test_hash_embedder_is_deterministic_normalised_and_lexical():
    e = es.HashEmbedder(64)
    a, b, c = e.embed(["total amount due 100 usd", "total amount due 250 usd", "completely unrelated words here"])
    assert e.embed(["total amount due 100 usd"])[0] == a and len(a) == 64
    assert abs(math.sqrt(sum(x * x for x in a)) - 1) < 1e-9
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(a, b) > dot(a, c)  # shared words -> closer
    assert e.embed([""])[0] == [0.0] * 64  # empty text does not crash


def test_providers(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "none")
    assert es.get_embedding_client() is None
    monkeypatch.setattr(settings, "embedding_provider", "hash")
    assert isinstance(es.get_embedding_client(), es.HashEmbedder)
    monkeypatch.setattr(settings, "embedding_provider", "mystery")
    with pytest.raises(ValueError):
        es.get_embedding_client()


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with Session(sa.create_engine(url)) as s:
        yield s
        s.rollback()
        s.execute(sa.delete(Document).where(Document.original_filename.like("emb-test-%")))
        s.commit()


def doc_with_pages(session, texts):
    d = Document(original_filename="emb-test-a.pdf", stored_path="/x", file_type="pdf", file_size=1,
                 file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), status=DocumentStatus.processing)
    session.add(d)
    session.flush()
    for i, t in enumerate(texts, 1):
        session.add(DocumentPage(document_id=d.id, page_number=i, extracted_text=t, extraction_method=ExtractionMethod.digital))
    session.commit()
    return d


def count(session, d):
    return session.scalar(sa.select(sa.func.count()).select_from(Embedding).where(Embedding.document_id == d.id))


def test_embed_document_is_idempotent_and_ordered(session):
    d = doc_with_pages(session, ["alpha " * 300, "", "beta " * 300])
    n = es.embed_document(session, d.id)
    assert n > 2 and count(session, d) == n
    assert es.embed_document(session, d.id) == n and count(session, d) == n  # second run replaces, not adds
    rows = session.scalars(sa.select(Embedding).where(Embedding.document_id == d.id).order_by(Embedding.chunk_index)).all()
    assert [r.chunk_index for r in rows] == list(range(n)) and "alpha" in rows[0].chunk_text and "beta" in rows[-1].chunk_text
    assert len(rows[0].embedding) == settings.embedding_dimension


def test_vector_search_finds_the_right_chunk(session):
    import numpy as np

    d = doc_with_pages(session, ["invoice total payable in euro", "shipping address and delivery notes"])
    es.embed_document(session, d.id)
    q = es.HashEmbedder(settings.embedding_dimension).embed(["delivery address"])[0]
    best = session.scalars(sa.select(Embedding).where(Embedding.document_id == d.id)
                           .order_by(Embedding.embedding.cosine_distance(q)).limit(1)).one()
    assert "delivery" in best.chunk_text


def test_provider_none_skips_and_failure_is_contained(session, monkeypatch):
    d = doc_with_pages(session, ["some text"])
    monkeypatch.setattr(settings, "embedding_provider", "none")
    assert es.embed_document(session, d.id) == 0 and count(session, d) == 0

    class Broken(es.EmbeddingClient):
        def embed(self, texts):
            raise RuntimeError("model down")

    assert es.embed_document(session, d.id, Broken()) == 0  # does not raise
    assert session.get(Document, d.id).status == DocumentStatus.processing  # document status untouched
