"""Celery task: retries with backoff, idempotency, progress. Runs eagerly (no Redis); needs TEST_DATABASE_URL."""
import os

import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from mocks import MockLLM, field, response
from sqlalchemy.orm import Session, sessionmaker

from app.core import db
from app.core.config import settings
from app.main import create_app
from app.models import (
    Document, DocumentPage, DocumentStatus, Embedding, ExtractedResult, ProcessingHistory,
)
from app.services import extraction_service, pipeline
from app.services.llm_client import LLMError, LLMTransientError
from app.workers import tasks
from app.workers.celery_app import celery_app, ping


@pytest.fixture
def env(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "task_backend", "celery")
    monkeypatch.setattr(settings, "task_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "llm_backoff_base_seconds", 0.0)
    from app.api import documents as api
    api.upload_limiter.reset()
    queued = []
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda *a: queued.append(a))
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield engine, queued
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()


def upload(env, name, path):
    engine, queued = env
    client = TestClient(create_app())
    r = client.post("/api/documents/upload", files=[("files", (name, open(path, "rb").read()))])
    assert r.status_code == 202, r.text
    return client, r.json()["files"][0]["id"], queued[-1]


def history(env, doc_id, stage=None):
    with Session(env[0]) as s:
        q = sa.select(ProcessingHistory).where(ProcessingHistory.document_id == doc_id).order_by(ProcessingHistory.started_at)
        if stage:
            q = q.where(ProcessingHistory.stage == stage)
        return s.scalars(q).all()


def test_celery_is_configured_for_safe_redelivery():
    c = celery_app.conf
    assert c.task_acks_late and c.task_reject_on_worker_lost and c.worker_prefetch_multiplier == 1
    assert "process_document" in celery_app.tasks and ping.apply().get() == "pong"


def test_the_worker_runs_all_stages_and_reports_progress(env):
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    assert args == (doc_id, 1) and client.get(f"/api/documents/{doc_id}/status").json()["status"] == "queued"
    tasks.process_document_task.apply(args=args)
    s = client.get(f"/api/documents/{doc_id}/status").json()
    assert (s["status"], s["stage"], s["progress"]) == ("completed", "done", 100)
    stages = [h.stage for h in history(env, doc_id)]
    assert stages == ["enqueue", "validate_file", "extract_text", "segment", "extract_fields", "save_results",
                      "validate", "embed"]
    assert all(h.status == "succeeded" and h.finished_at and h.duration_ms is not None for h in history(env, doc_id))


def test_progress_only_moves_forward_and_follows_pages(env, monkeypatch):
    import pymupdf

    engine, _ = env
    pdf = os.path.join(settings.upload_dir, "big.pdf")
    with pymupdf.open() as d:
        for i in range(60):
            d.new_page().insert_text((72, 72), f"Page {i} " + "text " * 40)
        d.save(pdf)
    seen = []
    real = pipeline.mark_progress
    monkeypatch.setattr(pipeline, "mark_progress", lambda s, doc, stage, pct: (seen.append((stage, int(pct))), real(s, doc, stage, pct))[1])
    with Session(engine) as s:
        doc = Document(original_filename="big.pdf", stored_path=pdf, file_type="pdf", file_size=1, file_hash="b" * 64)
        s.add(doc); s.commit()
        pipeline.process_document(s, doc.id)
    text = [p for st, p in seen if st == "extract_text"]
    assert text[0] == 5 and text[-1] == 50 and text == sorted(text)
    assert len(set(text)) > 3  # moved page batch by page batch, not in one jump
    assert [p for _, p in seen] == sorted(p for _, p in seen)  # overall: never goes backwards


def test_task_is_idempotent(env):
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    tasks.process_document_task.apply(args=args)
    tasks.process_document_task.apply(args=args)  # the same job delivered twice
    with Session(env[0]) as s:
        count = lambda m: s.scalar(sa.select(sa.func.count()).select_from(m).where(m.document_id == doc_id))
        assert count(DocumentPage) == 1 and count(ExtractedResult) == 1
        assert s.scalar(sa.select(sa.func.count()).select_from(Embedding).where(Embedding.document_id == doc_id)) >= 1
        n_emb = s.scalar(sa.select(sa.func.count()).select_from(Embedding).where(Embedding.document_id == doc_id))
    tasks.process_document_task.apply(args=args)
    with Session(env[0]) as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(Embedding).where(Embedding.document_id == doc_id)) == n_emb
        assert s.scalar(sa.select(sa.func.count()).select_from(ExtractedResult).where(ExtractedResult.document_id == doc_id)) == 1
    assert client.get(f"/api/documents/{doc_id}/status").json()["status"] == "completed"


def test_temporary_llm_outage_is_retried_three_times_then_failed(env, monkeypatch):
    llm = MockLLM([LLMTransientError("overloaded")])
    monkeypatch.setattr(extraction_service, "get_llm_client", lambda: llm)
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    tasks.process_document_task.apply(args=args)
    retries = history(env, doc_id, "retry")
    assert [h.details["retry"] for h in retries] == [1, 2, 3]
    assert [h.details["wait_seconds"] for h in retries] == [0.0, 0.0, 0.0]  # 0 here; 5, 10, 20 s in real use
    assert len(history(env, doc_id, "retry_exhausted")) == 1
    s = client.get(f"/api/documents/{doc_id}/status").json()
    assert s["status"] == "failed" and "unavailable" in s["error_message"].lower()
    assert len(llm.calls) == 4 * settings.llm_max_attempts  # first run + 3 retries, each with its own in-call retries


def test_backoff_doubles_and_stops_after_three_retries(monkeypatch):
    import uuid

    from app.services.orchestrator import RunResult

    monkeypatch.setattr(settings, "task_retry_backoff_seconds", 5.0)
    monkeypatch.setattr(tasks, "run_pipeline", lambda *a: RunResult(False, True, "temporary"))
    monkeypatch.setattr(tasks, "record_event", lambda *a, **k: None)
    waits = []

    class Stop(Exception):
        pass

    def fake_retry(**kw):
        waits.append(kw["countdown"])
        raise Stop

    monkeypatch.setattr(tasks.process_document_task, "retry", fake_retry)
    for n in (0, 1, 2, 3):
        tasks.process_document_task.push_request(retries=n)
        try:
            tasks.process_document_task.run(str(uuid.uuid4()), 1)
        except Stop:
            pass
        finally:
            tasks.process_document_task.pop_request()
    assert waits == [5.0, 10.0, 20.0]  # the 4th call (3 retries already used) gives up instead of retrying


def test_outage_that_ends_succeeds_on_retry(env, monkeypatch):
    good = response(invoice_number=field("INV-2026-0042", "Invoice number: INV-2026-0042"),
                    total=field("757.05", "TOTAL: 757.05 USD"))
    calls = {"n": 0}

    def handler(system, messages):
        calls["n"] += 1
        if calls["n"] <= settings.llm_max_attempts:  # the whole first run fails
            raise LLMTransientError("busy")
        return good

    llm = MockLLM(handler=handler)
    monkeypatch.setattr(extraction_service, "get_llm_client", lambda: llm)
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    tasks.process_document_task.apply(args=args)
    assert len(history(env, doc_id, "retry")) == 1 and not history(env, doc_id, "retry_exhausted")
    r = client.get(f"/api/documents/{doc_id}/result").json()
    assert r["results"][0]["fields"]["invoice_number"]["value"] == "INV-2026-0042"
    assert r["document"]["status"] in ("completed", "needs_review") and r["document"]["progress"] == 100
    with Session(env[0]) as s:  # one result set, not one per attempt
        assert s.scalar(sa.select(sa.func.count()).select_from(ExtractedResult).where(ExtractedResult.document_id == doc_id)) == 1


def test_permanent_failures_are_not_retried(env, monkeypatch):
    client, doc_id, args = upload(env, "corrupted.pdf", sample("corrupted.pdf"))
    tasks.process_document_task.apply(args=args)
    assert history(env, doc_id, "retry") == [] and client.get(f"/api/documents/{doc_id}/status").json()["status"] == "failed"

    monkeypatch.setattr(extraction_service, "get_llm_client", lambda: MockLLM([LLMError("invalid api key")]))
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    tasks.process_document_task.apply(args=args)
    assert history(env, doc_id, "retry") == []  # a bad key will not fix itself
    assert "invalid api key" in client.get(f"/api/documents/{doc_id}/status").json()["error_message"]


def test_deleted_document_is_skipped_quietly(env):
    client, doc_id, args = upload(env, "digital_invoice.pdf", sample("digital_invoice.pdf"))
    assert client.delete(f"/api/documents/{doc_id}").status_code == 204
    tasks.process_document_task.apply(args=args)  # the job arrives after the delete: no crash, no retry
    with Session(env[0]) as s:
        assert s.get(Document, doc_id) is None
