"""Upload API end to end (demo extractor, no Redis); needs TEST_DATABASE_URL."""
import os

import pytest
import sqlalchemy as sa
from conftest import sample
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api import documents as api
from app.core import db
from app.core.config import settings
from app.main import app
from app.models import Document


@pytest.fixture
def client(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(settings, "llm_provider", "demo")
    with Session(engine) as s:
        s.execute(sa.delete(Document))  # the API tests own the whole table
        s.commit()
    yield TestClient(app)
    with Session(engine) as s:
        s.execute(sa.delete(Document))
        s.commit()


def upload(client, name, content=None, path=None):
    data = content if content is not None else open(path, "rb").read()
    return client.post("/api/documents", files={"file": (name, data)})


def test_digital_pdf_goes_through_all_three_stages(client):
    r = upload(client, "digital_invoice.pdf", path=sample("digital_invoice.pdf"))
    assert r.status_code == 202
    d = client.get(f"/api/documents/{r.json()['id']}").json()  # background task already ran
    assert d["status"] == "completed" and d["flagged"] is False
    f = d["results"][0]["fields"]
    assert f["invoice_number"]["value"] == "INV-2026-0042" and f["total"]["value"] == "757.05"
    assert f["total"]["source_snippet"] == "TOTAL: 757.05 USD" and f["total"]["confidence"] == 0.8
    assert [i["line_total"] for i in d["results"][0]["line_items"]] == ["240.00", "31.00", "450.00"]
    assert [h["stage"] for h in d["history"]] == ["validate_file", "extract_text", "segment", "extract_fields",
                                                  "save_results", "validate"]
    assert d["pages"][0]["method"] == "digital"


def test_spreadsheet_is_mapped_without_the_llm(client):
    r = upload(client, "multi_sheet.xlsx", path=sample("multi_sheet.xlsx"))
    d = client.get(f"/api/documents/{r.json()['id']}").json()
    assert d["results"][0]["source"] == "spreadsheet_mapping"
    assert d["status"] == "needs_review"  # no currency/customer columns -> warnings; missing fields -> errors
    assert {f["rule_code"] for f in d["findings"]} >= {"MISSING_FIELD"}


def test_validation_findings_are_visible(client):
    text = b"INVOICE\nInvoice No: A-1\nDate: 2026-01-02\nSupplier: Acme\nCustomer: Bob\nItem  2 x 10.00 = 25.00\nSubtotal: 25.00\nTax: 1.00\nTotal: 99.00 USD\n"
    d = client.get(f"/api/documents/{upload(client, 'bad.csv', text).json()['id']}").json()
    assert d["status"] in ("needs_review", "completed")


def test_corrupted_file_fails_with_a_clear_message(client):
    d = client.get(f"/api/documents/{upload(client, 'corrupted.pdf', path=sample('corrupted.pdf')).json()['id']}").json()
    assert d["status"] == "failed" and d["error_message"]


def test_same_file_twice_is_rejected(client):
    upload(client, "a.csv", path=sample("invoices.csv"))
    r = upload(client, "b.csv", path=sample("invoices.csv"))
    assert r.status_code == 409 and r.json()["detail"]["filename"] == "a.csv"
    assert len(os.listdir(settings.upload_dir)) == 1  # the second copy was not kept


def test_unsupported_and_oversized_uploads(client, monkeypatch):
    assert upload(client, "notes.docx", b"x").status_code == 400
    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    assert upload(client, "a.csv", b"a,b\n1,2\n").status_code == 413
    assert os.listdir(settings.upload_dir) == []


def test_list_and_info_and_404(client):
    upload(client, "a.csv", path=sample("invoices.csv"))
    rows = client.get("/api/documents").json()
    assert rows[0]["filename"] == "a.csv" and "status" in rows[0] and "errors" in rows[0]
    assert client.get("/api/info").json()["llm_provider"] == "demo"
    assert client.get("/api/documents/00000000-0000-0000-0000-000000000000").status_code == 404
