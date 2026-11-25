"""Running anything twice must not create duplicates. Needs TEST_DATABASE_URL."""
import hashlib
import os
import uuid
from datetime import date
from decimal import Decimal as D

import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from mocks import MockLLM, field, response
from sqlalchemy.orm import Session, sessionmaker

from app.api import documents as api
from app.core import db
from app.core.config import settings
from app.main import create_app
from app.models import (
    Correction, Document, DocumentPage, DocumentStatus, Embedding, ExtractedResult, LineItem, ValidationError as VErr,
)
from app.services import embedding_service, extraction_service, pipeline, validation_service
from app.workers import tasks


@pytest.fixture
def env(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "task_backend", "inline")
    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 10_000)
    api.upload_limiter.reset()
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield engine
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()


def n(s, model, doc_id, col="document_id"):
    return s.scalar(sa.select(sa.func.count()).select_from(model).where(getattr(model, col) == doc_id))


def pdf_doc(s) -> Document:
    d = Document(original_filename="a.pdf", stored_path=sample("digital_invoice.pdf"), file_type="pdf", file_size=1,
                 file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest())
    s.add(d); s.commit()
    return d


GOOD = response(invoice_number=field("INV-2026-0042", "Invoice number: INV-2026-0042"),
                total=field("757.05", "TOTAL: 757.05 USD"),
                line_items=[{"description": "Web hosting (12 months)", "quantity": "1", "unit_price": "240.00", "line_total": "240.00",
                             "confidence": 0.9, "source_snippet": "Web hosting (12 months) 1 x 240.00 = 240.00"}])


def test_processing_a_document_twice_keeps_one_copy_of_each_page(env):
    with Session(env) as s:
        d = pdf_doc(s)
        for _ in range(3):
            assert pipeline.process_document(s, d.id).ok
        assert n(s, DocumentPage, d.id) == 1 and s.get(Document, d.id).page_count == 1


def test_extracting_the_same_version_twice_replaces_results_and_line_items(env):
    with Session(env) as s:
        d = pdf_doc(s)
        pipeline.process_document(s, d.id)
        for _ in range(3):
            extraction_service.extract_document(s, d.id, MockLLM([GOOD]), version=1)
        assert n(s, ExtractedResult, d.id) == 1
        assert s.scalar(sa.select(sa.func.count()).select_from(LineItem)) == 1  # not 3
        extraction_service.extract_document(s, d.id, MockLLM([GOOD]), version=2)  # a different attempt IS a new version
        assert sorted(s.scalars(sa.select(ExtractedResult.version).where(ExtractedResult.document_id == d.id))) == [1, 2]


def test_validating_twice_gives_the_same_findings_not_more(env):
    with Session(env) as s:
        d = pdf_doc(s)
        pipeline.process_document(s, d.id)
        extraction_service.extract_document(s, d.id, MockLLM([GOOD]), version=1)  # missing date, supplier: errors
        first = validation_service.validate_document(s, d.id)
        rows1 = sorted((f.rule_code, f.field, f.message) for f in s.scalars(sa.select(VErr).where(VErr.document_id == d.id)))
        second = validation_service.validate_document(s, d.id)
        validation_service.validate_document(s, d.id)
        rows2 = sorted((f.rule_code, f.field, f.message) for f in s.scalars(sa.select(VErr).where(VErr.document_id == d.id)))
        assert rows1 == rows2 and len(rows1) >= 1  # findings exist, and three runs left exactly one set
        assert (first.errors, first.warnings, first.status) == (second.errors, second.warnings, second.status)


def test_embedding_twice_keeps_the_same_chunks(env):
    with Session(env) as s:
        d = pdf_doc(s)
        pipeline.process_document(s, d.id)
        a = embedding_service.embed_document(s, d.id)
        ids1 = sorted(s.scalars(sa.select(Embedding.chunk_index).where(Embedding.document_id == d.id)))
        b = embedding_service.embed_document(s, d.id)
        assert a == b == n(s, Embedding, d.id) and ids1 == list(range(a))


def test_the_whole_task_three_times_leaves_exactly_one_of_everything(env, monkeypatch):
    client = TestClient(create_app())
    r = client.post("/api/documents/upload", files=[("files", ("a.pdf", open(sample("digital_invoice.pdf"), "rb").read()))])
    doc_id = r.json()["files"][0]["id"]  # (inline mode already ran it once)
    with Session(env) as s:
        before = (n(s, DocumentPage, doc_id), n(s, ExtractedResult, doc_id), n(s, Embedding, doc_id))
    for _ in range(3):
        tasks.process_document_task.apply(args=[doc_id, 1])
    with Session(env) as s:
        assert (n(s, DocumentPage, doc_id), n(s, ExtractedResult, doc_id), n(s, Embedding, doc_id)) == before
        assert n(s, ExtractedResult, doc_id) == 1 and s.get(Document, uuid.UUID(doc_id)).status == DocumentStatus.completed


def test_uploading_the_same_file_again_creates_nothing_new(env):
    client = TestClient(create_app())
    data = open(sample("invoices.csv"), "rb").read()
    first = client.post("/api/documents/upload", files=[("files", ("a.csv", data))])
    for name in ("again.csv", "AGAIN.CSV", "renamed.csv"):
        r = client.post("/api/documents/upload", files=[("files", (name, data))])
        assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_file"
    assert client.get("/api/documents").json()["total"] == 1 and len(os.listdir(settings.upload_dir)) == 1


def test_sending_the_same_correction_twice_logs_it_once(env):
    client = TestClient(create_app())
    r = client.post("/api/documents/upload", files=[("files", ("a.pdf", open(sample("digital_invoice.pdf"), "rb").read()))])
    doc_id = r.json()["files"][0]["id"]
    body = {"fields": {"customer_name": "Carol"}}
    assert client.patch(f"/api/documents/{doc_id}/result", json=body).status_code == 200
    assert client.patch(f"/api/documents/{doc_id}/result", json=body).status_code == 400  # already that value
    with Session(env) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Correction)) == 1


def test_reprocessing_twice_adds_versions_but_never_duplicates_within_a_version(env):
    client = TestClient(create_app())
    r = client.post("/api/documents/upload", files=[("files", ("c.pdf", open(sample("corrupted.pdf"), "rb").read()))])
    doc_id = r.json()["files"][0]["id"]
    for _ in range(2):
        assert client.post(f"/api/documents/{doc_id}/reprocess").status_code == 202
    with Session(env) as s:
        assert n(s, DocumentPage, doc_id) == 0  # a corrupted file never produced pages, however often retried
    assert client.get(f"/api/documents/{doc_id}/status").json()["attempts"] == 3
