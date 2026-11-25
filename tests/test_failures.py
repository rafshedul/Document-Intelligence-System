"""What happens when things break: Redis down, worker dies mid-job, LLM times out, corrupted files.
Needs TEST_DATABASE_URL. The point of every test: the system ends in a clear, recoverable state."""
import os
import random
import time

import anthropic
import httpx
import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from mocks import MockLLM
from sqlalchemy.orm import Session, sessionmaker

from app.api import documents as api
from app.core import db
from app.core.config import settings
from app.main import create_app
from app.models import Document, DocumentPage, DocumentStatus, Embedding, ExtractedResult, ProcessingHistory
from app.services import extraction_service, pdf_processor, pipeline
from app.services.llm_client import AnthropicLLMClient
from app.workers import tasks
from app.workers.celery_app import celery_app


class WorkerKilled(BaseException):
    """Stands in for the worker process dying: not an Exception, so nothing in the pipeline can catch it."""


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
    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 10_000)
    api.upload_limiter.reset()
    sent = []
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda *a: sent.append(a))
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield engine, sent
    with Session(engine) as s:
        s.execute(sa.delete(Document)); s.commit()


def upload(env, name, data):
    client = TestClient(create_app())
    r = client.post("/api/documents/upload", files=[("files", (name, data))])
    return client, r


def counts(engine, doc_id):
    with Session(engine) as s:
        n = lambda m, col: s.scalar(sa.select(sa.func.count()).select_from(m).where(col == doc_id))
        return {"pages": n(DocumentPage, DocumentPage.document_id), "results": n(ExtractedResult, ExtractedResult.document_id),
                "embeddings": n(Embedding, Embedding.document_id)}


def status(engine, doc_id):
    with Session(engine) as s:
        return s.get(Document, doc_id).status


# ---- Redis is down ----------------------------------------------------------------------------------------------------

@pytest.fixture
def redis_down(monkeypatch):
    """The real Celery client, pointed at a port where nothing listens: the genuine connection error, not a fake."""
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda *a: tasks.process_document_task.apply_async(args=a))  # the real publisher
    monkeypatch.setitem(celery_app.conf, "broker_url", "redis://127.0.0.1:1/0")
    monkeypatch.setitem(celery_app.conf, "broker_transport_options", {"socket_connect_timeout": 1, "max_retries": 1})
    celery_app._pool = None


def test_upload_while_redis_is_down_is_a_clean_503_and_nothing_stays_queued(env, redis_down):
    engine, _ = env
    t = time.time()
    client, r = upload(env, "a.csv", b"Invoice No,Total\nINV-1,10.00\n")
    assert time.time() - t < 5  # fails fast instead of hanging the request (it used to take ~20 s)
    assert r.status_code == 503 and r.json()["error"]["code"] == "queue_unavailable"
    assert "Traceback" not in r.text and "redis" not in r.text.lower()  # no internals in the message
    with Session(engine) as s:
        doc = s.scalars(sa.select(Document)).one()
        assert doc.status == DocumentStatus.failed and "queue" in doc.error_message.lower()
    assert client.get("/api/documents").status_code == 200  # reading still works without Redis
    assert client.get(f"/api/documents/{doc.id}/status").json()["status"] == "failed"


def test_reprocess_while_redis_is_down(env, redis_down, monkeypatch):
    engine, _ = env
    client, r = upload(env, "bad.pdf", open(sample("corrupted.pdf"), "rb").read())
    assert r.status_code == 503
    doc_id = client.get("/api/documents").json()["items"][0]["id"]
    r = client.post(f"/api/documents/{doc_id}/reprocess")
    assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"
    assert client.get(f"/api/documents/{doc_id}/status").json()["status"] == "failed"  # still failed, still reprocessable


def test_health_reports_redis_down(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    from app.api import health

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q):
            class R:
                def fetchone(s): return (1,)
            return R()

    monkeypatch.setattr(health.psycopg, "connect", lambda *a, **k: FakeConn())
    r = TestClient(create_app()).get("/health")
    assert r.status_code == 503 and r.json()["redis"] is False and r.json()["status"] == "degraded"


# ---- the worker dies in the middle of a job --------------------------------------------------------------------------------

def make_pdf(path, pages):
    import pymupdf

    with pymupdf.open() as d:
        for i in range(pages):
            d.new_page().insert_text((72, 72), f"Invoice No: INV-{i}\n" + "line of text " * 30)
        d.save(path)


def test_worker_killed_while_reading_pages_then_job_redelivered(env, monkeypatch, tmp_path):
    engine, sent = env
    path = tmp_path / "big.pdf"
    make_pdf(str(path), 60)
    client, r = upload(env, "big.pdf", path.read_bytes())
    args = sent[-1]
    real = pdf_processor._extract_page
    monkeypatch.setattr(pdf_processor, "_extract_page", lambda doc, i, info: (_ for _ in ()).throw(WorkerKilled()) if i == 30 else real(doc, i, info))

    with pytest.raises(WorkerKilled):
        tasks.process_document_task.apply(args=args)
    doc_id = args[0]
    assert counts(engine, doc_id)["pages"] == 25  # the first batch was saved before the "crash"
    assert status(engine, doc_id) == DocumentStatus.processing  # left mid-way, as after a real kill

    monkeypatch.setattr(pdf_processor, "_extract_page", real)  # a healthy worker picks the job up again
    tasks.process_document_task.apply(args=args)
    c = counts(engine, doc_id)
    assert c["pages"] == 60 and c["results"] >= 1  # all pages, none twice (page numbers are unique)
    assert status(engine, doc_id) in (DocumentStatus.completed, DocumentStatus.needs_review)
    assert client.get(f"/api/documents/{doc_id}/status").json()["progress"] == 100


@pytest.mark.parametrize("dies_in", ["extract_document", "validate_document", "embed_document"])
def test_worker_killed_between_stages_then_redelivered_without_duplicates(env, monkeypatch, dies_in):
    engine, sent = env
    client, r = upload(env, "digital_invoice.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    args = sent[-1]
    from app.services import embedding_service, orchestrator, validation_service

    target = {"extract_document": (extraction_service, "extract_document"), "validate_document": (validation_service, "validate_document"),
              "embed_document": (embedding_service, "embed_document")}[dies_in]
    real = getattr(*target)
    monkeypatch.setattr(target[0], target[1], lambda *a, **k: (_ for _ in ()).throw(WorkerKilled()))
    with pytest.raises(WorkerKilled):
        tasks.process_document_task.apply(args=args)
    monkeypatch.setattr(target[0], target[1], real)
    tasks.process_document_task.apply(args=args)
    c = counts(engine, args[0])
    assert c["pages"] == 1 and c["results"] == 1 and c["embeddings"] >= 1
    with Session(engine) as s:
        n = lambda m: s.scalar(sa.select(sa.func.count()).select_from(m).where(m.document_id == args[0]))
        assert n(ExtractedResult) == 1
    assert status(engine, args[0]) == DocumentStatus.completed


def test_celery_keeps_unfinished_jobs_until_they_finish():
    c = celery_app.conf
    assert c.task_acks_late is True and c.task_reject_on_worker_lost is True  # a dead worker's job is given to another
    assert c.broker_transport_options["visibility_timeout"] >= 2 * 3600  # long OCR jobs are not handed out twice


# ---- the LLM times out --------------------------------------------------------------------------------------------------------

def fake_sdk_client(error):
    class Messages:
        calls = 0

        def create(self, **kw):
            Messages.calls += 1
            raise error

    c = AnthropicLLMClient.__new__(AnthropicLLMClient)
    c._anthropic, c.model = anthropic, "claude-opus-5-5"
    c._client = type("C", (), {"beta": type("B", (), {"messages": Messages()})()})()
    return c, Messages


def test_llm_timeouts_are_retried_then_the_document_fails_with_a_clear_message(env, monkeypatch):
    engine, sent = env
    llm, messages = fake_sdk_client(anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")))
    monkeypatch.setattr(extraction_service, "get_llm_client", lambda: llm)
    client, r = upload(env, "digital_invoice.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    tasks.process_document_task.apply(args=sent[-1])
    s = client.get(f"/api/documents/{sent[-1][0]}/status").json()
    assert s["status"] == "failed" and "unavailable" in s["error_message"].lower() and "Traceback" not in s["error_message"]
    assert messages.calls == 4 * settings.llm_max_attempts  # every attempt really tried the LLM
    with Session(engine) as s2:
        stages = [h.stage for h in s2.scalars(sa.select(ProcessingHistory).where(ProcessingHistory.document_id == sent[-1][0]))]
    assert stages.count("retry") == 3 and stages.count("retry_exhausted") == 1
    r = client.post(f"/api/documents/{sent[-1][0]}/reprocess")  # and it can be tried again later
    assert r.status_code == 503 or r.status_code == 202


# ---- corrupted files, every supported type ----------------------------------------------------------------------------------

def noise(n=3000, seed=1):
    return random.Random(seed).randbytes(n)


def truncated(path, fraction=0.5):
    data = open(path, "rb").read()
    return data[: int(len(data) * fraction)]


def xls_bytes():
    import io
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("S")
    for i in range(300):
        ws.write(i, 0, f"row {i}"); ws.write(i, 1, i)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def tiff_bytes():
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.open(sample("scanned_invoice.jpg")).convert("L").save(buf, "TIFF")
    return buf.getvalue()


CASES = [
    ("noise.pdf", noise()), ("noise.xlsx", noise()), ("noise.xls", noise()), ("noise.csv", b"\x00\x01\x02" + noise()),
    ("noise.jpg", noise()), ("noise.png", noise()), ("noise.tiff", noise()),
    ("cut.pdf", truncated(sample("scanned_invoice.pdf"), 0.4)), ("cut.xlsx", truncated(sample("multi_sheet.xlsx"), 0.5)),
    ("cut.xls", xls_bytes()[:700]), ("cut.jpg", truncated(sample("scanned_invoice.jpg"), 0.3)),
    ("cut.tiff", tiff_bytes()[:600]), ("empty.pdf", b""), ("empty.csv", b"\n\n  \n"), ("only-header.pdf", b"%PDF-1.7\n"),
    ("png-as.jpg", b"\x89PNG\r\n\x1a\n" + noise(500)), ("zip.xlsx", b"PK\x03\x04" + noise(200)),
]


@pytest.mark.parametrize("name,data", CASES, ids=[c[0] for c in CASES])
def test_corrupted_input_ends_in_a_clear_final_state_never_stuck_or_crashing(env, name, data, tmp_path):
    engine, _ = env
    path = tmp_path / "stored"
    path.write_bytes(data)
    with Session(engine) as s:
        doc = Document(original_filename=name, stored_path=str(path), file_type="x", file_size=len(data), file_hash=os.urandom(32).hex())
        s.add(doc); s.commit()
        out = pipeline.process_document(s, doc.id)  # must not raise
        s.refresh(doc)
        if not out.ok:  # rejected: failed, with a message a person can read
            assert doc.status == DocumentStatus.failed and doc.error_message and "Traceback" not in doc.error_message
            assert out.error_code and not out.retryable  # a damaged file will not get better by retrying
        else:  # some damaged files are still partly readable; then nothing may be half-saved
            assert doc.status == DocumentStatus.processing and doc.page_count == s.scalar(
                sa.select(sa.func.count()).select_from(DocumentPage).where(DocumentPage.document_id == doc.id))
