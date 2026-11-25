"""REST API through TestClient: every endpoint, success and failure. Needs TEST_DATABASE_URL.
Background work runs inline (TASK_BACKEND=inline, demo extractor), so results are ready after the call."""
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api import documents as api
from app.core import db
from app.core.config import settings
from app.main import create_app
from app.models import Correction, Document, DocumentPage, DocumentStatus, Embedding, ExtractedResult, ProcessingHistory

INVOICE = (b"INVOICE\nInvoice No: {n}\nDate: 2026-01-02\nSupplier: Acme\nCustomer: Bob\n"
           b"Item A  2 x 10.00 = 20.00\nItem B  1 x 5.00 = 5.00\nSubtotal: 25.00\nTax: 1.25\nTotal: 26.25 USD\n")


def csv_invoice(n: str) -> bytes:  # a distinct, valid one-invoice CSV (different bytes -> different hash)
    return f"Invoice No,Date,Supplier,Customer,Total,Currency\n{n},2026-01-02,Acme,Bob,26.25,USD\n".encode()


@pytest.fixture
def engine(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    eng = sa.create_engine(url)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(eng, expire_on_commit=False))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "llm_provider", "demo")
    monkeypatch.setattr(settings, "task_backend", "inline")
    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 1000)
    api.upload_limiter.reset()
    with Session(eng) as s:
        s.execute(sa.delete(Document)); s.commit()
    yield eng
    with Session(eng) as s:
        s.execute(sa.delete(Document)); s.commit()


@pytest.fixture
def client(engine):
    return TestClient(create_app())


@pytest.fixture
def sql(engine):
    with Session(engine) as s:
        yield s


def up(client, *files):
    """files: (name, bytes)"""
    return client.post("/api/documents/upload", files=[("files", f) for f in files])


def up_one(client, name, content):
    r = up(client, (name, content))
    assert r.status_code == 202, r.text
    return r.json()["files"][0]["id"]


def err(r, status, code):
    assert r.status_code == status, r.text
    e = r.json()["error"]
    assert e["code"] == code and e["message"] and e["request_id"]
    assert "Traceback" not in r.text
    return e


# ---- 1. POST /api/documents/upload ---------------------------------------------------------------

def test_upload_many_files_at_once(client):
    pdf = open(sample("digital_invoice.pdf"), "rb").read()
    r = up(client, ("a.pdf", pdf), ("b.csv", csv_invoice("B-1")), ("c.xlsx", open(sample("multi_sheet.xlsx"), "rb").read()))
    assert r.status_code == 202
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (3, 0)
    assert all(f["status"] == "queued" and f["id"] and f["error"] is None for f in body["files"])
    assert [client.get(f"/api/documents/{f['id']}/status").status_code for f in body["files"]] == [200] * 3


def test_upload_response_does_not_wait_for_ai(client, monkeypatch):
    monkeypatch.setattr(settings, "task_backend", "celery")
    sent = []
    from app.workers import tasks
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda *a: sent.append(a))
    r = up(client, ("a.csv", csv_invoice("X-1")))
    doc_id = r.json()["files"][0]["id"]
    assert sent == [(doc_id, 1)]  # handed to Celery with the attempt number
    s = client.get(f"/api/documents/{doc_id}/status").json()
    assert (s["status"], s["stage"], s["progress"]) == ("queued", "queued", 0)
    assert client.get(f"/api/documents/{doc_id}/result").json()["results"] == []  # nothing extracted in the request


def test_one_bad_file_does_not_stop_the_others(client):
    good = csv_invoice("G-1")
    up_one(client, "first.csv", good)
    r = up(client, ("new.csv", csv_invoice("G-2")), ("notes.docx", b"PK\x03\x04x"), ("again.csv", good))
    assert r.status_code == 202
    b = r.json()
    assert (b["accepted"], b["rejected"]) == (1, 2)
    codes = {f["filename"]: f["error"]["code"] for f in b["files"] if f["error"]}
    assert codes == {"notes.docx": "unsupported_format", "again.csv": "duplicate_file"}
    dup = next(f for f in b["files"] if f["filename"] == "again.csv")["error"]
    assert dup["http_status"] == 409 and dup["existing_document_id"]


def test_all_files_rejected_returns_the_error_format(client):
    e = err(up(client, ("notes.docx", b"x")), 415, "unsupported_format")
    assert e["details"]["files"][0]["status"] == "rejected"
    e = err(up(client, ("a.csv", b"")), 400, "empty_file")
    assert "empty" in e["message"].lower()


def test_duplicate_file_is_409_and_not_kept(client, engine):
    up_one(client, "a.csv", csv_invoice("D-1"))
    e = err(up(client, ("copy.csv", csv_invoice("D-1"))), 409, "duplicate_file")
    assert e["details"]["files"][0]["error"]["existing_document_id"]
    assert len(os.listdir(settings.upload_dir)) == 1


def test_mislabelled_file_is_rejected_at_upload(client):
    e = err(up(client, ("photo.png", open(sample("digital_invoice.pdf"), "rb").read())), 415, "extension_mismatch")
    assert "PDF" in e["message"] and os.listdir(settings.upload_dir) == []
    assert client.get("/api/documents").json()["total"] == 0


def test_file_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    err(up(client, ("a.csv", csv_invoice("S-1"))), 413, "file_too_large")
    assert os.listdir(settings.upload_dir) == []


def test_size_limit_at_the_real_boundary(client, monkeypatch):
    monkeypatch.setattr(settings, "max_file_size_mb", 1)
    row = b"Invoice No,Total\nINV-1,10.00\n"
    under = row + b"#" * (1024 * 1024 - len(row))          # exactly 1 MB: allowed
    over = row + b"#" * (1024 * 1024 - len(row) + 1)       # one byte more: refused
    assert up(client, ("under.csv", under)).status_code == 202
    from app.services import file_router
    detected = []
    real = file_router.detect
    monkeypatch.setattr(file_router, "detect", lambda *a, **k: (detected.append(a), real(*a, **k))[1])
    err(up(client, ("over.csv", over)), 413, "file_too_large")
    assert detected == []  # stopped while streaming, before the whole file was written and inspected
    assert len(os.listdir(settings.upload_dir)) == 1  # the oversized file was not kept


def test_too_many_files_and_no_files(client, monkeypatch):
    monkeypatch.setattr(settings, "max_files_per_upload", 2)
    err(up(client, *[(f"{i}.csv", csv_invoice(str(i))) for i in range(3)]), 400, "bad_request")
    e = err(client.post("/api/documents/upload"), 422, "validation_error")
    assert e["details"][0]["field"] == "files"


def test_filename_cannot_escape_the_upload_folder(client, sql):
    r = up(client, ("../../etc/passwd.csv", csv_invoice("P-1")))
    assert r.status_code == 202 and r.json()["files"][0]["filename"] == "passwd.csv"
    stored = sql.scalar(sa.select(Document.stored_path))
    assert os.path.dirname(stored) == settings.upload_dir and "passwd" not in os.path.basename(stored)
    assert not os.path.exists("/etc/passwd.csv") and not os.path.exists(os.path.join(settings.upload_dir, "..", "..", "etc"))


def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 3)
    api.upload_limiter.reset()
    assert up(client, ("1.csv", csv_invoice("R-1")), ("2.csv", csv_invoice("R-2"))).status_code == 202
    r = up(client, ("3.csv", csv_invoice("R-3")), ("4.csv", csv_invoice("R-4")))
    e = err(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1 and e["message"]
    assert client.get("/api/documents").json()["total"] == 2  # the blocked request saved nothing


# ---- 2. GET /api/documents ------------------------------------------------------------------------

def test_list_filters_and_pagination(client, sql):
    ids = [up_one(client, f"{n}.csv", csv_invoice(n)) for n in ("L-1", "L-2", "L-3", "L-4", "L-5")]
    up_one(client, "x.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    body = client.get("/api/documents").json()
    assert body["total"] == 6 and body["page"] == 1 and body["page_size"] == 20 and body["pages"] == 1
    p1 = client.get("/api/documents", params={"page_size": 4}).json()
    p2 = client.get("/api/documents", params={"page_size": 4, "page": 2}).json()
    assert (len(p1["items"]), len(p2["items"]), p1["pages"]) == (4, 2, 2)
    assert not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}  # pages do not overlap
    assert {i["file_type"] for i in client.get("/api/documents", params={"file_type": "pdf"}).json()["items"]} == {"pdf"}
    assert client.get("/api/documents", params={"file_type": ".CSV"}).json()["total"] == 5
    sql.execute(sa.update(Document).where(Document.id == ids[0]).values(status=DocumentStatus.failed))
    sql.commit()
    failed = client.get("/api/documents", params={"status": "failed"}).json()
    assert [i["id"] for i in failed["items"]] == [ids[0]]
    assert client.get("/api/documents", params={"flagged": "true"}).json()["total"] >= 0
    now = sql.scalar(sa.select(sa.func.now()))
    future = (now.replace(year=now.year + 1)).isoformat()
    assert client.get("/api/documents", params={"created_from": future}).json()["total"] == 0
    assert client.get("/api/documents", params={"created_to": future}).json()["total"] == 6
    item = p1["items"][0]
    assert {"id", "filename", "status", "stage", "progress", "file_type", "created_at", "errors", "warnings"} <= set(item)


def test_list_empty_and_invalid_parameters(client):
    assert client.get("/api/documents").json() == {"items": [], "total": 0, "page": 1, "page_size": 20, "pages": 0}
    for params in ({"page": 0}, {"page_size": 101}, {"page_size": 0}, {"status": "nope"}, {"created_from": "yesterday"}):
        err(client.get("/api/documents", params=params), 422, "validation_error")


# ---- 3. GET /api/documents/{id}/status ----------------------------------------------------------------

def test_status_of_a_finished_document(client):
    i = up_one(client, "a.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    s = client.get(f"/api/documents/{i}/status").json()
    assert s["status"] == "completed" and s["stage"] == "done" and s["progress"] == 100
    assert s["error_message"] is None and s["attempts"] == 1 and s["updated_at"]


def test_status_of_a_failed_document_keeps_the_stage_and_message(client):
    i = up_one(client, "bad.pdf", open(sample("corrupted.pdf"), "rb").read())
    s = client.get(f"/api/documents/{i}/status").json()
    assert s["status"] == "failed" and s["stage"] == "extract_text" and 5 <= s["progress"] < 100
    assert s["error_message"]


def test_status_not_found_and_bad_id(client):
    err(client.get("/api/documents/00000000-0000-0000-0000-000000000000/status"), 404, "not_found")
    err(client.get("/api/documents/not-a-uuid/status"), 422, "validation_error")


# ---- 4. GET /api/documents/{id}/result ------------------------------------------------------------------

def test_result_has_data_findings_and_history(client):
    i = up_one(client, "a.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    r = client.get(f"/api/documents/{i}/result").json()
    assert r["document"]["status"] == "completed" and r["document"]["filename"] == "a.pdf"
    inv = r["results"][0]
    assert inv["fields"]["invoice_number"]["value"] == "INV-2026-0042"
    assert inv["fields"]["total"] == {"value": "757.05", "confidence": 0.8, "source_snippet": "TOTAL: 757.05 USD", "manual": False}
    assert [l["line_total"] for l in inv["line_items"]] == ["240.00", "31.00", "450.00"]
    assert r["findings"] == []
    stages = [h["stage"] for h in r["history"]]
    assert stages[0] == "enqueue" and {"extract_text", "extract_fields", "validate", "embed"} <= set(stages)
    assert all(h["status"] in ("succeeded", "started") for h in r["history"])


def test_result_lists_findings_for_a_bad_invoice(client):
    bad = INVOICE.replace(b"{n}", b"B-1").replace(b"Total: 26.25", b"Total: 99.00")
    i = up_one(client, "bad.csv", bad)
    r = client.get(f"/api/documents/{i}/result").json()
    assert r["document"]["status"] in ("needs_review", "completed")
    assert isinstance(r["findings"], list)


def test_result_not_found(client):
    err(client.get("/api/documents/00000000-0000-0000-0000-000000000000/result"), 404, "not_found")


# ---- 5. PATCH /api/documents/{id}/result ---------------------------------------------------------------------

def make_invoice_doc(client, number="C-1"):
    """A completed document from the text format the demo extractor reads."""
    return up_one(client, f"{number}.csv", csv_invoice(number))


def patch(client, doc_id, **body):
    return client.patch(f"/api/documents/{doc_id}/result", json=body)


def test_correction_saves_audit_rows_and_reruns_validation(client, sql):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-9"))
    sql.execute(sa.delete(Correction)); sql.commit()
    before = client.get(f"/api/documents/{i}/result").json()
    r = patch(client, i, fields={"total": "30.00", "customer_name": "Carol"}, corrected_by="rafshedul")
    assert r.status_code == 200, r.text
    after = r.json()
    f = after["results"][0]["fields"]
    assert f["total"]["value"] == "30.00" and f["total"]["manual"] and f["total"]["confidence"] == 1.0
    assert after["results"][0]["is_manually_corrected"] is True
    rows = sql.scalars(sa.select(Correction).order_by(Correction.field)).all()
    assert [(c.field, c.old_value, c.new_value, c.corrected_by) for c in rows] == [
        ("customer_name", before["results"][0]["fields"]["customer_name"]["value"], "Carol", "rafshedul"),
        ("total", before["results"][0]["fields"]["total"]["value"], "30.00", "rafshedul")]
    assert rows[0].corrected_at is not None
    assert any(f["rule_code"] == "TOTAL_MISMATCH" for f in after["findings"])  # validation ran again: 25.00 + 1.25 != 30.00
    assert after["document"]["status"] == "needs_review"
    fixed = patch(client, i, fields={"total": "26.25"}).json()
    assert not any(x["rule_code"] == "TOTAL_MISMATCH" for x in fixed["findings"])  # and the fix clears it


def test_correcting_line_items_and_clearing_a_value(client, sql):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-8"))
    r = patch(client, i, fields={"customer_name": None}, line_items=[{"position": 1, "line_total": "21.00"}])
    assert r.status_code == 200, r.text
    inv = r.json()["results"][0]
    assert inv["fields"]["customer_name"]["value"] is None
    assert inv["line_items"][0]["line_total"] == "21.00" and inv["line_items"][0]["quantity"] == "2.000"
    assert any(f["rule_code"] == "LINE_ITEM_MISMATCH" for f in r.json()["findings"])
    fields = {c.field for c in sql.scalars(sa.select(Correction))}
    assert {"customer_name", "line_items[1].line_total"} <= fields


@pytest.mark.parametrize("fields,field", [
    ({"total": "abc"}, "fields.total"), ({"total": "1.234"}, "fields.total"), ({"total": "NaN"}, "fields.total"),
    ({"invoice_date": "03/04/2026"}, "fields.invoice_date"), ({"invoice_date": "2026-02-30"}, "fields.invoice_date"),
    ({"currency": "DOLLAR"}, "fields.currency"), ({"invoice_number": "x" * 200}, "fields.invoice_number"),
])
def test_invalid_correction_values_are_422_and_change_nothing(client, sql, fields, field):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-7"))
    e = err(patch(client, i, fields=fields), 422, "validation_error")
    assert e["details"]["problems"][0]["field"] == field
    assert sql.scalar(sa.select(sa.func.count()).select_from(Correction)) == 0


def test_correction_request_errors(client):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-6"))
    err(patch(client, i, fields={"nonsense": "1"}), 422, "validation_error")
    err(patch(client, i, extra=1), 422, "validation_error")
    err(patch(client, i, fields={"total": "26.25"}), 400, "bad_request")  # same as stored: nothing to change
    err(patch(client, i, line_items=[{"position": 9, "description": "x"}]), 422, "validation_error")
    err(patch(client, i, invoice_index=5, fields={"tax": "1.00"}), 404, "not_found")
    err(patch(client, "00000000-0000-0000-0000-000000000000", fields={"tax": "1"}), 404, "not_found")


def test_cannot_correct_while_processing_or_without_a_result(client, sql, monkeypatch):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-5"))
    sql.execute(sa.update(Document).where(Document.id == i).values(status=DocumentStatus.processing)); sql.commit()
    err(patch(client, i, fields={"tax": "1.00"}), 409, "conflict")
    sql.execute(sa.update(Document).where(Document.id == i).values(status=DocumentStatus.failed))
    sql.execute(sa.delete(ExtractedResult)); sql.commit()
    err(patch(client, i, fields={"tax": "1.00"}), 404, "not_found")


# ---- 6. POST /api/documents/{id}/reprocess -----------------------------------------------------------------------

def test_reprocess_a_failed_document_and_count_attempts(client):
    i = up_one(client, "bad.pdf", open(sample("corrupted.pdf"), "rb").read())
    assert client.get(f"/api/documents/{i}/status").json()["status"] == "failed"
    r = client.post(f"/api/documents/{i}/reprocess")
    assert r.status_code == 202 and r.json() == {"id": i, "status": "queued", "attempt": 2}
    s = client.get(f"/api/documents/{i}/status").json()
    assert s["attempts"] == 2 and s["status"] == "failed"  # inline run failed again, same reason
    enq = [h for h in client.get(f"/api/documents/{i}/result").json()["history"] if h["stage"] == "enqueue"]
    assert [h["details"] for h in enq] == [{"attempt": 1, "reason": "upload"}, {"attempt": 2, "reason": "reprocess"}]


def test_reprocess_a_needs_review_document_makes_a_new_version(client, sql):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-4").replace(b"Total: 26.25", b"Total: 99.00"))
    assert client.get(f"/api/documents/{i}/status").json()["status"] == "needs_review"
    assert client.post(f"/api/documents/{i}/reprocess").status_code == 202
    assert sorted(sql.scalars(sa.select(ExtractedResult.version))) == [1, 2]
    assert client.get(f"/api/documents/{i}/result").json()["results"][0]["version"] == 2


def test_reprocess_is_refused_for_other_states(client, sql):
    i = up_one(client, "ok.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    assert client.get(f"/api/documents/{i}/status").json()["status"] == "completed"
    e = err(client.post(f"/api/documents/{i}/reprocess"), 409, "conflict")
    assert "completed" in e["message"]
    err(client.post("/api/documents/00000000-0000-0000-0000-000000000000/reprocess"), 404, "not_found")


def test_reprocess_when_the_queue_is_down(client, sql, monkeypatch):
    i = up_one(client, "bad.pdf", open(sample("corrupted.pdf"), "rb").read())
    monkeypatch.setattr(settings, "task_backend", "celery")
    from app.workers import tasks
    monkeypatch.setattr(tasks.process_document_task, "delay", lambda *a: (_ for _ in ()).throw(ConnectionError("redis")))
    err(client.post(f"/api/documents/{i}/reprocess"), 503, "service_unavailable")
    assert client.get(f"/api/documents/{i}/status").json()["status"] == "failed"


# ---- 7. DELETE /api/documents/{id} ---------------------------------------------------------------------------------

def test_delete_removes_everything(client, sql):
    i = up_one(client, "a.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    path = sql.scalar(sa.select(Document.stored_path).where(Document.id == i))
    assert os.path.exists(path)
    assert sql.scalar(sa.select(sa.func.count()).select_from(Embedding)) > 0
    r = client.delete(f"/api/documents/{i}")
    assert r.status_code == 204 and r.content == b""
    assert not os.path.exists(path)
    for model in (DocumentPage, ExtractedResult, ProcessingHistory, Embedding):
        assert sql.scalar(sa.select(sa.func.count()).select_from(model)) == 0, model
    err(client.get(f"/api/documents/{i}/status"), 404, "not_found")
    err(client.delete(f"/api/documents/{i}"), 404, "not_found")


def test_cannot_delete_while_processing(client, sql):
    i = up_one(client, "a.csv", csv_invoice("DEL-1"))
    sql.execute(sa.update(Document).where(Document.id == i).values(status=DocumentStatus.processing)); sql.commit()
    err(client.delete(f"/api/documents/{i}"), 409, "conflict")
    assert client.get(f"/api/documents/{i}/status").status_code == 200


# ---- errors, OpenAPI -------------------------------------------------------------------------------------------------

def test_every_kind_of_error_has_the_same_shape(client):
    err(client.get("/api/nothing-here"), 404, "not_found")
    err(client.put("/api/documents"), 405, "method_not_allowed")
    err(client.get("/api/documents/xyz"), 422, "validation_error")


def test_unexpected_errors_do_not_leak_details(engine, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail: password=hunter2")

    monkeypatch.setattr(api, "attempts_so_far", boom)
    c = TestClient(create_app(), raise_server_exceptions=False)
    i = up_one(c, "a.pdf", open(sample("corrupted.pdf"), "rb").read())
    r = c.get(f"/api/documents/{i}/status")
    e = err(r, 500, "internal_error")
    assert e["message"] == "Internal server error." and "hunter2" not in r.text and "secret" not in r.text


def test_request_id_is_returned_and_echoed(client):
    r = client.get("/api/documents")
    assert len(r.headers["x-request-id"]) >= 8
    assert client.get("/api/documents", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"


def test_openapi_docs_describe_every_endpoint(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    ops = {(m.upper(), p) for p, item in spec["paths"].items() for m in item if m in ("get", "post", "patch", "delete")}
    assert {("POST", "/api/documents/upload"), ("GET", "/api/documents"), ("GET", "/api/documents/{document_id}/status"),
            ("GET", "/api/documents/{document_id}/result"), ("PATCH", "/api/documents/{document_id}/result"),
            ("POST", "/api/documents/{document_id}/reprocess"), ("DELETE", "/api/documents/{document_id}")} <= ops
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert "ErrorResponse" in spec["components"]["schemas"]
    assert spec["paths"]["/api/documents/upload"]["post"]["summary"]


def test_docs_can_be_switched_off(engine, monkeypatch):
    monkeypatch.setattr(settings, "docs_enabled", False)
    c = TestClient(create_app())
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


# ---- wrong or missing credentials must leave no trace ---------------------------------------------------------------

@pytest.fixture
def secured(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", "s" * 32)
    return TestClient(create_app())


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + "s" * 32}])
def test_an_unauthorised_upload_stores_nothing(secured, sql, headers):
    r = secured.post("/api/documents/upload", headers=headers, files=[("files", ("a.csv", csv_invoice("U-1")))])
    err(r, 401, "unauthorized")
    assert os.listdir(settings.upload_dir) == [] and sql.scalar(sa.select(sa.func.count()).select_from(Document)) == 0


def test_an_unauthorised_delete_or_patch_changes_nothing(secured, client, sql):
    good = {"Authorization": "Bearer " + "s" * 32}
    i = secured.post("/api/documents/upload", headers=good, files=[("files", ("a.csv", csv_invoice("U-2")))]).json()["files"][0]["id"]
    assert secured.delete(f"/api/documents/{i}").status_code == 401
    assert secured.patch(f"/api/documents/{i}/result", json={"fields": {"tax": "1.00"}}).status_code == 401
    assert secured.delete(f"/api/documents/{i}", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert secured.get(f"/api/documents/{i}/status", headers=good).status_code == 200  # still there
    assert secured.delete(f"/api/documents/{i}", headers=good).status_code == 204


# ---- remaining endpoint and failure paths ------------------------------------------------------------------------------

def test_document_detail_for_the_web_page(client):
    i = up_one(client, "a.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    d = client.get(f"/api/documents/{i}").json()
    assert d["document"]["id"] == i and d["results"][0]["fields"]["invoice_number"]["value"] == "INV-2026-0042"
    assert d["pages"][0]["page"] == 1 and "INVOICE" in d["pages"][0]["text"] and d["pages"][0]["method"] == "digital"
    assert {"history", "findings"} <= set(d)
    err(client.get("/api/documents/00000000-0000-0000-0000-000000000000"), 404, "not_found")
    err(client.get("/api/documents/xyz"), 422, "validation_error")


def test_page_text_in_the_detail_view_is_capped(client, sql):
    i = up_one(client, "a.csv", csv_invoice("CAP-1"))
    sql.execute(sa.update(DocumentPage).where(DocumentPage.document_id == i).values(extracted_text="x" * 50_000))
    sql.commit()
    assert len(client.get(f"/api/documents/{i}").json()["pages"][0]["text"]) == api.PAGE_TEXT_LIMIT


def test_an_invalid_line_item_correction_is_rejected(client, sql):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"C-3"))
    e = err(patch(client, i, line_items=[{"position": 1, "quantity": "abc"}]), 422, "validation_error")
    assert e["details"]["problems"][0]["field"] == "line_items.1.quantity"
    err(patch(client, i, line_items=[{"position": 1, "unit_price": "1.234"}]), 422, "validation_error")  # 3 decimals
    assert sql.scalar(sa.select(sa.func.count()).select_from(Correction)) == 0


def test_an_unexpected_error_during_upload_returns_500_and_leaves_no_file(engine, monkeypatch):
    from app.services import file_router

    def boom(*a, **k):
        raise OSError("disk exploded: /secret/path")

    monkeypatch.setattr(file_router, "detect", boom)
    c = TestClient(create_app(), raise_server_exceptions=False)
    r = c.post("/api/documents/upload", files=[("files", ("a.csv", csv_invoice("X-9")))])
    err(r, 500, "internal_error")
    assert "disk exploded" not in r.text and "/secret" not in r.text
    assert os.listdir(settings.upload_dir) == []  # the half-saved file was cleaned up


def test_deleting_a_document_whose_file_is_already_gone_still_works(client, sql):
    i = up_one(client, "a.csv", csv_invoice("GONE-1"))
    os.remove(sql.scalar(sa.select(Document.stored_path).where(Document.id == i)))
    assert client.delete(f"/api/documents/{i}").status_code == 204
    err(client.get(f"/api/documents/{i}/status"), 404, "not_found")


def test_the_server_refuses_to_start_in_production_without_a_token(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", "")
    with pytest.raises(RuntimeError, match="API_AUTH_TOKEN"):
        with TestClient(create_app()):  # entering the context runs the startup check
            pass
    monkeypatch.setattr(settings, "api_auth_token", "z" * 32)
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code in (200, 503)


# ---- original file (preview) and correction log --------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected,inline", [
    ("digital_invoice.pdf", "application/pdf", True), ("scanned_invoice.jpg", "image/jpeg", True)])
def test_the_original_file_is_served_for_preview(client, name, expected, inline):
    data = open(sample(name), "rb").read()
    i = up_one(client, name, data)
    r = client.get(f"/api/documents/{i}/file")
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == expected
    assert r.headers["content-disposition"].startswith("inline; filename*=UTF-8''" + name)
    assert "sandbox" in r.headers["content-security-policy"] and "script" not in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_non_previewable_files_are_downloads_and_download_can_be_forced(client):
    i = up_one(client, "inv.csv", csv_invoice("F-1"))
    r = client.get(f"/api/documents/{i}/file")
    assert r.headers["content-type"].startswith("text/csv") and r.headers["content-disposition"].startswith("attachment")
    j = up_one(client, "x.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    assert client.get(f"/api/documents/{j}/file", params={"download": "true"}).headers["content-disposition"].startswith("attachment")


def test_the_served_type_comes_from_the_detected_format_not_the_filename(client, sql):
    i = up_one(client, "a.pdf", open(sample("digital_invoice.pdf"), "rb").read())
    sql.execute(sa.update(Document).where(Document.id == i).values(original_filename="evil<script>.html"))
    sql.commit()
    r = client.get(f"/api/documents/{i}/file")
    assert r.headers["content-type"] == "application/pdf" and "<script>" not in r.headers["content-disposition"]  # percent-encoded


def test_file_endpoint_errors(client, sql):
    err(client.get("/api/documents/00000000-0000-0000-0000-000000000000/file"), 404, "not_found")
    i = up_one(client, "a.csv", csv_invoice("F-2"))
    os.remove(sql.scalar(sa.select(Document.stored_path).where(Document.id == i)))
    e = err(client.get(f"/api/documents/{i}/file"), 404, "not_found")
    assert "missing" in e["message"] and "/tmp" not in e["message"]


def test_correction_log_lists_every_change_newest_first(client):
    i = up_one(client, "inv.csv", INVOICE.replace(b"{n}", b"H-1"))
    assert client.get(f"/api/documents/{i}/corrections").json() == []
    patch(client, i, fields={"customer_name": "Carol"}, corrected_by="alice")
    patch(client, i, fields={"total": "30.00"}, corrected_by="bob")
    log = client.get(f"/api/documents/{i}/corrections").json()
    assert [(c["field"], c["new_value"], c["corrected_by"]) for c in log] == [("total", "30.00", "bob"), ("customer_name", "Carol", "alice")]
    assert log[0]["old_value"] == "26.25" and log[0]["invoice_index"] == 0 and log[0]["version"] == 1 and log[0]["corrected_at"]
    err(client.get("/api/documents/00000000-0000-0000-0000-000000000000/corrections"), 404, "not_found")
