"""End-to-end pipeline tests; need TEST_DATABASE_URL (a migrated Postgres)."""
import hashlib
import os
import shutil
import uuid

import pytest
import sqlalchemy as sa
from conftest import needs_tesseract, sample
from sqlalchemy.orm import Session

from app.models import Document, DocumentPage, DocumentStatus, ProcessingHistory
from app.services import file_router, pipeline


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = sa.create_engine(url)
    with Session(engine) as s:
        yield s
        s.rollback()
        s.execute(sa.delete(Document).where(Document.original_filename.like("pipeline-test-%")))
        s.commit()


@pytest.fixture
def make_doc(session, tmp_path):
    def _make(src: str, name: str | None = None, content: bytes | None = None) -> Document:
        dest = tmp_path / f"{uuid.uuid4().hex}"
        if content is None:
            shutil.copy(src, dest)
        else:
            dest.write_bytes(content)
        doc = Document(
            original_filename="pipeline-test-" + (name or os.path.basename(src)),
            stored_path=str(dest), file_type="unknown", file_size=dest.stat().st_size,
            file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        )
        session.add(doc)
        session.commit()
        return doc
    return _make


def history(session, doc):
    return session.scalars(
        sa.select(ProcessingHistory).where(ProcessingHistory.document_id == doc.id).order_by(ProcessingHistory.started_at)
    ).all()


def test_digital_pdf_end_to_end(session, make_doc):
    doc = make_doc(sample("digital_invoice.pdf"), "digital_invoice.pdf")
    out = pipeline.process_document(session, doc.id)
    assert out.ok and out.page_count == 1
    session.refresh(doc)
    assert doc.status == DocumentStatus.processing and doc.page_count == 1 and doc.file_type == "pdf"
    page = session.scalars(sa.select(DocumentPage).where(DocumentPage.document_id == doc.id)).one()
    assert "INV-2026-0042" in page.extracted_text
    stages = history(session, doc)
    assert [(h.stage, h.status) for h in stages] == [("validate_file", "succeeded"), ("extract_text", "succeeded")]
    assert all(h.finished_at and h.duration_ms is not None for h in stages)
    assert stages[1].details["pages_saved"] == 1


@needs_tesseract
def test_scanned_pdf_saves_ocr_confidence(session, make_doc):
    doc = make_doc(sample("scanned_invoice.pdf"), "scanned_invoice.pdf")
    assert pipeline.process_document(session, doc.id).ok
    page = session.scalars(sa.select(DocumentPage).where(DocumentPage.document_id == doc.id)).one()
    assert page.extraction_method.value == "ocr" and float(page.ocr_confidence) > 80


def test_spreadsheet_end_to_end(session, make_doc):
    doc = make_doc(sample("multi_sheet.xlsx"), "multi_sheet.xlsx")
    out = pipeline.process_document(session, doc.id)
    assert out.ok and out.page_count == 2


def test_rerun_replaces_pages(session, make_doc):
    doc = make_doc(sample("invoices.csv"), "invoices.csv")
    pipeline.process_document(session, doc.id)
    pipeline.process_document(session, doc.id)
    n = session.scalar(sa.select(sa.func.count()).select_from(DocumentPage).where(DocumentPage.document_id == doc.id))
    assert n == 1


@pytest.mark.parametrize("name,content,code", [
    ("corrupted.pdf", None, {"corrupted_file", "empty_file"}),
    ("empty.pdf", b"", {"empty_file"}),
    ("photo.png", b"%PDF-1.4 not really", {"extension_mismatch"}),
    ("report.docx", b"PK\x03\x04", {"unsupported_format"}),
    ("a.csv", b"   \n", {"empty_file"}),
])
def test_bad_files_fail_cleanly(session, make_doc, name, content, code):
    src = sample("corrupted.pdf") if content is None else None
    doc = make_doc(src, name, content)
    out = pipeline.process_document(session, doc.id)
    assert not out.ok and out.error_code in code
    session.refresh(doc)
    assert doc.status == DocumentStatus.failed and doc.error_message == out.error_message
    last = history(session, doc)[-1]
    assert last.status == "failed" and last.details["error_code"] == out.error_code


def test_unexpected_bug_is_contained(session, make_doc, monkeypatch):
    doc = make_doc(sample("invoices.csv"), "invoices.csv")

    class Boom:
        @staticmethod
        def process(path):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(file_router, "get_processor", lambda kind: Boom)
    out = pipeline.process_document(session, doc.id)
    assert not out.ok and out.error_code == "internal_error"
    assert "kaboom" not in out.error_message  # internals are not leaked to users
    session.refresh(doc)
    assert doc.status == DocumentStatus.failed
    assert "kaboom" in history(session, doc)[-1].details["traceback"]
