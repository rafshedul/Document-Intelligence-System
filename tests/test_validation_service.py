"""validate_document and the database rules (6 DUPLICATE_*, 9 CROSS_DOCUMENT); needs TEST_DATABASE_URL."""
import hashlib
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    Document, DocumentStatus, ExtractedResult, LineItem, ProcessingHistory, ValidationError as VErr,
)
from app.services import validation_service as vs

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    with Session(engine) as s:
        yield s
        s.rollback()
        s.execute(sa.delete(Document).where(Document.original_filename.like("val-test-%")))
        s.commit()


_clock = iter(range(10_000))


def make(session, name="a.pdf", file_type="pdf", file_hash=None, **result) -> Document:
    """A document with one extracted result. Later calls count as uploaded later."""
    doc = Document(original_filename="val-test-" + name, stored_path="/x", file_type=file_type, file_size=1,
                   file_hash=file_hash or hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                   status=DocumentStatus.processing, created_at=T0 + timedelta(minutes=next(_clock)))
    session.add(doc)
    session.flush()
    fields = dict(invoice_number="INV-1", invoice_date=date(2026, 1, 5), supplier_name="Acme Ltd",
                  customer_name="Rahim", subtotal=D("100.00"), tax=D("5.00"), total=D("105.00"), currency="USD",
                  field_confidence={"invoice_number": {"confidence": 0.95}}, version=1, invoice_index=0)
    items = result.pop("items", [(1, "A", "2", "30.00", "60.00"), (2, "B", "1", "40.00", "40.00")])
    fields.update(result)
    r = ExtractedResult(document_id=doc.id, **fields)
    r.line_items = [LineItem(position=p, description=d, quantity=D(q), unit_price=D(u), line_total=D(t))
                    for p, d, q, u, t in items]
    session.add(r)
    session.commit()
    return doc


def findings(session, doc, rule=None):
    q = sa.select(VErr).where(VErr.document_id == doc.id)
    if rule:
        q = q.where(VErr.rule_code == rule)
    return session.scalars(q.order_by(VErr.rule_code, VErr.field)).all()


def run(session, doc):
    out = vs.validate_document(session, doc.id, today=TODAY)
    session.refresh(doc)
    return out


# ---- final status ---------------------------------------------------------------

def test_clean_document_is_completed_and_not_flagged(session):
    doc = make(session)
    out = run(session, doc)
    assert (doc.status, doc.is_flagged, doc.error_message) == (DocumentStatus.completed, False, None)
    assert out.errors == 0 and out.warnings == 0 and findings(session, doc) == []


def test_warnings_only_means_completed_but_flagged(session):
    doc = make(session, customer_name=None)  # optional field missing -> warning
    out = run(session, doc)
    assert (doc.status, doc.is_flagged) == (DocumentStatus.completed, True)
    assert (out.errors, out.warnings) == (0, 1)
    f = findings(session, doc)[0]
    assert (f.rule_code, f.severity.value, f.field, f.resolved) == ("MISSING_FIELD", "warning", "customer_name", False)
    assert f.result_id is not None


def test_any_error_means_needs_review(session):
    doc = make(session, total=D("999.00"))
    out = run(session, doc)
    assert doc.status == DocumentStatus.needs_review and "1 validation error" in doc.error_message
    assert [f.rule_code for f in findings(session, doc)] == ["TOTAL_MISMATCH"] and out.by_rule == {"TOTAL_MISMATCH": 1}


def test_findings_get_all_saved_columns_and_stage_is_logged(session):
    doc = make(session, subtotal=D("500.00"), total=D("505.00"), invoice_number=None)
    run(session, doc)
    rules = {f.rule_code for f in findings(session, doc)}
    assert rules == {"MISSING_FIELD", "SUBTOTAL_MISMATCH"}
    assert all(f.message and f.field for f in findings(session, doc))
    h = session.scalars(sa.select(ProcessingHistory).where(ProcessingHistory.document_id == doc.id,
                                                          ProcessingHistory.stage == "validate")).one()
    assert h.status == "succeeded" and h.details["errors"] == 2 and h.duration_ms is not None


def test_revalidation_replaces_old_findings_but_keeps_resolved_ones(session):
    doc = make(session, total=D("999.00"), customer_name=None)
    run(session, doc)
    warn = findings(session, doc, "MISSING_FIELD")[0]
    warn.resolved = True  # a reviewer dismissed it
    session.commit()
    r = session.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == doc.id)).one()
    r.total = D("105.00")  # the reviewer fixed the total
    session.commit()
    run(session, doc)
    assert doc.status == DocumentStatus.completed and doc.is_flagged is False
    left = findings(session, doc)
    assert [(f.rule_code, f.resolved) for f in left] == [("MISSING_FIELD", True)]  # not re-created


def test_extraction_errors_also_block_completion(session):
    doc = make(session)
    r = session.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == doc.id)).one()
    session.add(VErr(document_id=doc.id, result_id=r.id, rule_code="EXTRACT_LLM_OUTPUT_INVALID",
                     severity="error", message="bad"))
    session.commit()
    run(session, doc)
    assert doc.status == DocumentStatus.needs_review
    assert findings(session, doc, "EXTRACT_LLM_OUTPUT_INVALID")  # not deleted by validation


def test_every_invoice_in_a_document_is_checked(session):
    doc = make(session)
    session.add(ExtractedResult(document_id=doc.id, invoice_index=1, version=1, invoice_number="INV-2",
                                supplier_name="Acme Ltd", invoice_date=date(2026, 1, 6), total=D("5")))
    session.commit()
    run(session, doc)
    second = session.scalars(sa.select(ExtractedResult).where(ExtractedResult.invoice_index == 1,
                                                              ExtractedResult.document_id == doc.id)).one()
    assert {f.field for f in findings(session, doc) if f.result_id == second.id} == {"customer_name", "currency"}
    assert doc.status == DocumentStatus.completed and doc.is_flagged


def test_only_latest_version_is_validated(session):
    doc = make(session, total=D("999.00"))
    session.add(ExtractedResult(document_id=doc.id, version=2, invoice_number="INV-1", invoice_date=date(2026, 1, 5),
                                supplier_name="Acme Ltd", customer_name="R", currency="USD", total=D("5.00")))
    session.commit()
    run(session, doc)
    assert doc.status == DocumentStatus.completed  # the bad v1 total is history


def test_no_results_and_failed_documents(session):
    doc = make(session)
    session.execute(sa.delete(ExtractedResult).where(ExtractedResult.document_id == doc.id))
    session.commit()
    assert run(session, doc).status == DocumentStatus.needs_review
    doc.status = DocumentStatus.failed
    session.commit()
    assert run(session, doc).status is None and doc.status == DocumentStatus.failed


def test_a_crashing_rule_does_not_break_the_pipeline(session, monkeypatch):
    doc = make(session)
    monkeypatch.setattr(vs, "PURE_RULES", [lambda inv: 1 / 0])
    assert run(session, doc).status == DocumentStatus.needs_review
    assert "could not be completed" in doc.error_message


# ---- 6. DUPLICATE_INVOICE / DUPLICATE_FILE --------------------------------------------

def test_duplicate_invoice_flags_the_later_upload_only(session):
    first = make(session, "first.pdf")
    second = make(session, "second.pdf", supplier_name="  ACME  ltd. ")  # same supplier after normalising
    run(session, first)
    run(session, second)
    assert first.status == DocumentStatus.completed
    assert second.status == DocumentStatus.needs_review
    f = findings(session, second, "DUPLICATE_INVOICE")
    assert len(f) == 1 and "val-test-first.pdf" in f[0].message and f[0].severity.value == "error"


def test_same_number_different_supplier_is_not_a_duplicate(session):
    make(session, "a.pdf")
    other = make(session, "b.pdf", supplier_name="Globex")
    run(session, other)
    assert findings(session, other, "DUPLICATE_INVOICE") == []


def test_pdf_and_spreadsheet_copy_of_one_invoice_is_not_a_duplicate(session):
    make(session, "a.pdf")
    sheet = make(session, "a.xlsx", file_type="xlsx")
    run(session, sheet)
    assert findings(session, sheet, "DUPLICATE_INVOICE") == [] and findings(session, sheet, "CROSS_DOCUMENT") == []


def test_two_spreadsheets_with_the_same_invoice_are_duplicates(session):
    make(session, "a.xlsx", file_type="xlsx")
    b = make(session, "b.csv", file_type="csv")
    run(session, b)
    assert len(findings(session, b, "DUPLICATE_INVOICE")) == 1


def test_duplicate_inside_one_document(session):
    doc = make(session)
    session.add(ExtractedResult(document_id=doc.id, invoice_index=1, version=1, invoice_number="inv-1",
                                supplier_name="Acme Ltd", invoice_date=date(2026, 1, 5), total=D("5"),
                                customer_name="x", currency="USD"))
    session.commit()
    run(session, doc)
    f = findings(session, doc, "DUPLICATE_INVOICE")
    assert len(f) == 1 and "this document" in f[0].message


def test_missing_number_or_supplier_cannot_be_a_duplicate(session):
    make(session, "a.pdf")
    b = make(session, "b.pdf", invoice_number=None)
    run(session, b)
    assert findings(session, b, "DUPLICATE_INVOICE") == []


def test_duplicate_file_rule(session):
    """documents.file_hash is unique today, so drop that index inside a rolled-back transaction."""
    engine = session.get_bind()
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(sa.text("DROP INDEX uq_documents_file_hash"))
        s = Session(conn, join_transaction_mode="create_savepoint")
        h = hashlib.sha256(b"same").hexdigest()
        a = make(s, "x1.pdf", file_hash=h)
        b = make(s, "x2.pdf", file_hash=h)
        found = vs.rule_duplicate_file(s, b)
        assert [f.rule_code for f in found] == ["DUPLICATE_FILE"] and "val-test-x1.pdf" in found[0].message
        assert vs.rule_duplicate_file(s, make(s, "x3.pdf")) == []
        s.close()
        trans.rollback()


# ---- 9. CROSS_DOCUMENT ----------------------------------------------------------------

def test_pdf_and_excel_that_agree_pass(session):
    make(session, "a.pdf")
    x = make(session, "a.xlsx", file_type="xlsx", total=D("105.01"))  # within rounding
    run(session, x)
    assert findings(session, x, "CROSS_DOCUMENT") == [] and x.status == DocumentStatus.completed


def test_total_mismatch_between_pdf_and_excel(session):
    make(session, "a.pdf")
    x = make(session, "a.xlsx", file_type="xlsx", total=D("150.00"), subtotal=D("100.00"), tax=D("50.00"))
    run(session, x)
    f = findings(session, x, "CROSS_DOCUMENT")
    assert [(g.field, g.severity.value) for g in f] == [("total", "error")] and "val-test-a.pdf" in f[0].message
    assert x.status == DocumentStatus.needs_review


def test_supplier_mismatch_between_pdf_and_excel(session):
    make(session, "a.pdf")
    x = make(session, "a.xlsx", file_type="xlsx", supplier_name="Globex Inc")
    run(session, x)
    assert [g.field for g in findings(session, x, "CROSS_DOCUMENT")] == ["supplier_name"]


def test_pdf_vs_pdf_is_cross_checked_too(session):
    make(session, "a.pdf")
    b = make(session, "b.pdf", total=D("999.00"), subtotal=D("994.00"))
    run(session, b)
    assert {g.rule_code for g in findings(session, b)} >= {"CROSS_DOCUMENT", "DUPLICATE_INVOICE"}


def test_two_spreadsheets_are_not_cross_checked(session):
    make(session, "a.xlsx", file_type="xlsx")
    b = make(session, "b.xlsx", file_type="xlsx", total=D("150.00"), tax=D("50.00"))
    run(session, b)
    assert findings(session, b, "CROSS_DOCUMENT") == []


def test_unknown_values_are_not_mismatches(session):
    make(session, "a.pdf")
    x = make(session, "a.xlsx", file_type="xlsx", supplier_name=None, total=None, subtotal=None, tax=None)
    run(session, x)
    assert findings(session, x, "CROSS_DOCUMENT") == []


def test_different_invoice_numbers_are_unrelated(session):
    make(session, "a.pdf")
    x = make(session, "b.xlsx", file_type="xlsx", invoice_number="INV-2", total=D("1.00"), subtotal=D("1.00"), tax=D("0"))
    run(session, x)
    assert findings(session, x, "CROSS_DOCUMENT") == []
