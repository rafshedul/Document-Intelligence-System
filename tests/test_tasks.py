"""Celery task + dispatch; needs TEST_DATABASE_URL. Celery runs eagerly (no Redis in tests)."""
import os

import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core import db
from app.core.config import settings
from app.main import app
from app.models import Document
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
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield engine
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()


def test_celery_is_configured_for_safe_redelivery():
    c = celery_app.conf
    assert c.task_acks_late and c.task_reject_on_worker_lost and c.worker_prefetch_multiplier == 1
    assert "process_document" in celery_app.tasks and ping.apply().get() == "pong"


def test_task_runs_the_whole_pipeline(env, monkeypatch):
    monkeypatch.setattr(settings, "task_backend", "inline")
    client = TestClient(app)
    # upload without processing: pretend the queue swallowed the job
    monkeypatch.setattr(settings, "task_backend", "celery")
    queued = []
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda doc_id: queued.append(doc_id))
    r = client.post("/api/documents", files={"file": ("digital_invoice.pdf", open(sample("digital_invoice.pdf"), "rb").read())})
    assert r.status_code == 202 and queued == [r.json()["id"]]  # handed to Celery, not run in the request
    assert client.get(f"/api/documents/{queued[0]}").json()["status"] == "queued"

    tasks.process_document_task.apply(args=[queued[0]])  # what the worker does
    d = client.get(f"/api/documents/{queued[0]}").json()
    assert d["status"] == "completed" and d["results"][0]["fields"]["invoice_number"]["value"] == "INV-2026-0042"


def test_task_is_safe_to_run_twice(env, monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(settings, "task_backend", "inline")
    doc_id = client.post("/api/documents", files={"file": ("i.csv", open(sample("invoices.csv"), "rb").read())}).json()["id"]
    tasks.process_document_task.apply(args=[doc_id])
    d = client.get(f"/api/documents/{doc_id}").json()
    assert d["page_count"] == 1 and len(d["pages"]) == 1  # pages replaced, not duplicated


def test_queue_outage_fails_the_upload_cleanly(env, monkeypatch):
    monkeypatch.setattr(settings, "task_backend", "celery")

    def down(_):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(tasks.process_document_task, "delay", down)
    client = TestClient(app)
    r = client.post("/api/documents", files={"file": ("a.csv", b"a,b\n1,2\n")})
    assert r.status_code == 503
    row = client.get("/api/documents").json()[0]
    assert row["status"] == "failed"  # not stuck in "queued" forever
