"""extract_document end-to-end with a mock LLM; needs TEST_DATABASE_URL."""
import hashlib
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from conftest import sample
from mocks import MockLLM, field, response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Document, DocumentPage, DocumentStatus, ExtractedResult, ExtractionMethod, ProcessingHistory,
    ValidationError as VErr,
)
from app.services import extraction_service as es
from app.services import pipeline
from app.services.llm_client import LLMError, LLMUnavailableError


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    with Session(engine) as s:
        yield s
        s.rollback()
        s.execute(sa.delete(Document).where(Document.original_filename.like("extract-test-%")))
        s.commit()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(settings, "llm_backoff_base_seconds", 0.0)


def make_doc(session, pages, method=ExtractionMethod.digital, name="doc.pdf") -> Document:
    doc = Document(original_filename="extract-test-" + name, stored_path="/x", file_type="pdf", file_size=1,
                   file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), status=DocumentStatus.processing)
    session.add(doc)
    session.flush()
    for i, text in enumerate(pages, start=1):
        session.add(DocumentPage(document_id=doc.id, page_number=i, extracted_text=text, extraction_method=method))
    session.commit()
    return doc


def invoice_text(n: int) -> str:
    return f"INVOICE\nInvoice No: INV-{n:04d}\nSupplier: Acme\nItem A   1 x 10.00 = 10.00\nTotal: 10.00 USD\n"


def answer_for(text: str) -> dict:
    """What a perfect LLM would say about invoice_text()."""
    n = text.split("Invoice No: ")[1].split("\n")[0]
    return response(
        invoice_number=field(n, f"Invoice No: {n}", 0.98), supplier_name=field("Acme", "Supplier: Acme"),
        total=field("10.00", "Total: 10.00 USD"), currency=field("USD", "Total: 10.00 USD"),
        line_items=[{"description": "Item A", "quantity": "1", "unit_price": "10.00", "line_total": "10.00",
                     "confidence": 0.9, "source_snippet": "Item A   1 x 10.00 = 10.00"}])


def results(session, doc):
    return session.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == doc.id)
                           .order_by(ExtractedResult.version, ExtractedResult.invoice_index)).all()


def test_real_pdf_through_both_stages(session):
    doc = Document(original_filename="extract-test-digital_invoice.pdf", stored_path=sample("digital_invoice.pdf"),
                   file_type="pdf", file_size=1, file_hash=hashlib.sha256(b"real").hexdigest())
    session.add(doc)
    session.commit()
    assert pipeline.process_document(session, doc.id).ok
    text = session.scalar(sa.select(DocumentPage.extracted_text).where(DocumentPage.document_id == doc.id))
    llm = MockLLM([response(
        invoice_number=field("INV-2026-0042", "Invoice number: INV-2026-0042"),
        invoice_date=field("2026-11-24", "Invoice date: 2026-11-24"),
        total=field("757.05", "TOTAL: 757.05 USD"), currency=field("USD", "TOTAL: 757.05 USD"))])
    out = es.extract_document(session, doc.id, llm)
    assert out.ok and out.invoices == 1 and out.needs_review == 0
    r = results(session, doc)[0]
    assert (r.invoice_number, r.invoice_date, r.total, r.currency) == ("INV-2026-0042", date(2026, 11, 24),
                                                                       Decimal("757.05"), "USD")
    assert r.supplier_name is None  # not reported by the LLM: stays null
    assert r.field_confidence["total"]["source_snippet"] == "TOTAL: 757.05 USD"
    assert r.raw_llm_output["attempts"][0]["output"] and r.raw_llm_output["prompt"] == "extraction_v1"
    assert (r.page_start, r.page_end, r.version, r.invoice_index, r.is_manually_corrected) == (1, 1, 1, 0, False)
    stages = [h.stage for h in session.scalars(sa.select(ProcessingHistory).where(
        ProcessingHistory.document_id == doc.id).order_by(ProcessingHistory.started_at))]
    assert stages == ["validate_file", "extract_text", "segment", "extract_fields", "save_results"]
    session.refresh(doc)
    assert doc.status == DocumentStatus.processing  # validation stage comes later


def test_many_invoices_in_one_document_each_get_their_own_call_and_row(session):
    n = 30
    doc = make_doc(session, [invoice_text(i) for i in range(1, n + 1)])
    llm = MockLLM(handler=lambda s, m: answer_for(m[0]["content"]), delay=0.01)
    out = es.extract_document(session, doc.id, llm)
    assert out.invoices == n and len(llm.calls) == n
    rs = results(session, doc)
    assert [r.invoice_number for r in rs] == [f"INV-{i:04d}" for i in range(1, n + 1)]
    assert [(r.page_start, r.page_end, r.invoice_index) for r in rs[:2]] == [(1, 1, 0), (2, 2, 1)]
    assert all(len(r.line_items) == 1 and r.line_items[0].line_total == Decimal("10.00") for r in rs)
    assert llm.max_active <= settings.llm_concurrency  # bounded concurrency


def test_concurrency_limit_is_respected(session, monkeypatch):
    monkeypatch.setattr(settings, "llm_concurrency", 2)
    doc = make_doc(session, [invoice_text(i) for i in range(1, 11)])
    llm = MockLLM(handler=lambda s, m: answer_for(m[0]["content"]), delay=0.03)
    es.extract_document(session, doc.id, llm)
    assert llm.max_active == 2


def test_invalid_output_marks_document_needs_review_and_keeps_a_row(session):
    doc = make_doc(session, [invoice_text(1), invoice_text(2)])

    def handler(system, messages):
        return "garbage" if "INV-0002" in messages[0]["content"] else answer_for(messages[0]["content"])

    out = es.extract_document(session, doc.id, MockLLM(handler=handler))
    assert out.ok and out.invoices == 2 and out.needs_review == 1
    session.refresh(doc)
    assert doc.status == DocumentStatus.needs_review and "1 of 2" in doc.error_message
    good, bad = results(session, doc)
    assert good.invoice_number == "INV-0001"
    assert bad.invoice_number is None and bad.total is None and bad.page_start == 2
    assert len(bad.raw_llm_output["attempts"]) == 3  # raw outputs kept for the reviewer
    err = session.scalars(sa.select(VErr).where(VErr.result_id == bad.id)).one()
    assert err.rule_code == "EXTRACT_LLM_OUTPUT_INVALID" and err.severity.value == "error" and not err.resolved


def test_discarded_values_are_recorded_as_warnings(session):
    doc = make_doc(session, [invoice_text(1)])
    fake = response(invoice_number=field("INV-0001", "Invoice No: INV-0001"), tax=field("1.00", "Tax: 1.00"))
    es.extract_document(session, doc.id, MockLLM([fake]))
    r = results(session, doc)[0]
    assert r.tax is None and r.invoice_number == "INV-0001"
    w = session.scalars(sa.select(VErr).where(VErr.result_id == r.id)).one()
    assert (w.rule_code, w.field, w.severity.value) == ("EXTRACT_SNIPPET_NOT_FOUND", "tax", "warning")


def test_spreadsheet_with_known_columns_needs_no_llm(session):
    text = ("# Sheet: Invoices (rows 1-3)\nInvoice\tDate\tSupplier\tTotal\n"
            "INV-001\t2026-01-05\tAcme Hosting Ltd\t1250.5\nINV-002\t2026-02-11\tAcme Hosting Ltd\t310\n")
    doc = make_doc(session, [text], ExtractionMethod.spreadsheet, "inv.xlsx")
    llm = MockLLM([LLMError("must not be called")])
    out = es.extract_document(session, doc.id, llm)
    assert out.ok and out.invoices == 2 and llm.calls == []
    a, b = results(session, doc)
    assert (a.invoice_number, a.invoice_date, a.supplier_name, a.total) == (
        "INV-001", date(2026, 1, 5), "Acme Hosting Ltd", Decimal("1250.5"))
    assert b.total == Decimal("310") and a.currency is None
    assert a.raw_llm_output["source"] == "spreadsheet_mapping"
    assert a.field_confidence["total"]["source_snippet"] == "1250.5"


def test_spreadsheet_with_unknown_columns_uses_the_llm(session):
    text = "# Sheet: Data (rows 1-2)\nRef\tStuff\nINV-77\tbuy: 5.00 total\n"
    doc = make_doc(session, [text], ExtractionMethod.spreadsheet, "x.xlsx")
    llm = MockLLM([response(invoice_number=field("INV-77", "INV-77\tbuy"))])
    es.extract_document(session, doc.id, llm)
    assert len(llm.calls) == 1 and results(session, doc)[0].invoice_number == "INV-77"


def test_mixed_sheets_map_one_and_send_other(session):
    t1 = "# Sheet: A (rows 1-2)\nInvoice\tTotal\nINV-1\t5.00\n"
    t2 = "# Sheet: B (rows 1-2)\nNotes\nINV-9 for 7.00 total\n"
    doc = make_doc(session, [t1, t2], ExtractionMethod.spreadsheet, "m.xlsx")
    llm = MockLLM([response(invoice_number=field("INV-9", "INV-9 for 7.00 total"))])
    out = es.extract_document(session, doc.id, llm)
    assert out.invoices == 2 and len(llm.calls) == 1
    assert [r.invoice_number for r in results(session, doc)] == ["INV-1", "INV-9"]


def test_provider_failure_fails_the_document_cleanly(session):
    doc = make_doc(session, [invoice_text(i) for i in range(1, 4)])
    out = es.extract_document(session, doc.id, MockLLM([LLMError("invalid api key")]))
    assert not out.ok
    session.refresh(doc)
    assert doc.status == DocumentStatus.failed and "invalid api key" in doc.error_message
    assert results(session, doc) == []
    assert session.scalars(sa.select(ProcessingHistory).where(
        ProcessingHistory.document_id == doc.id, ProcessingHistory.status == "failed")).first()


def test_empty_text_needs_review_without_calling_the_llm(session):
    doc = make_doc(session, ["", "  "])
    llm = MockLLM([response()])
    out = es.extract_document(session, doc.id, llm)
    session.refresh(doc)
    assert not out.ok and doc.status == DocumentStatus.needs_review and llm.calls == []


def test_rerun_creates_a_new_version_and_keeps_the_old_one(session):
    doc = make_doc(session, [invoice_text(1)])
    llm = MockLLM(handler=lambda s, m: answer_for(m[0]["content"]))
    es.extract_document(session, doc.id, llm)
    es.extract_document(session, doc.id, llm)
    assert [(r.version, r.invoice_index) for r in results(session, doc)] == [(1, 0), (2, 0)]


def test_unexpected_bug_does_not_crash(session, monkeypatch):
    doc = make_doc(session, [invoice_text(1)])
    monkeypatch.setattr(es, "extract_segment", lambda *a, **k: 1 / 0)
    out = es.extract_document(session, doc.id, MockLLM([response()]))
    session.refresh(doc)
    assert not out.ok and doc.status == DocumentStatus.failed and "unexpected" in doc.error_message.lower()
