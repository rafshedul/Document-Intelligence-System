"""Orchestrates text extraction for one document and logs every stage.

process_document() never raises for a bad file: it marks the document `failed`
with a readable message and returns. Unexpected bugs are also contained (and
logged with a traceback) so one document can't take down the worker.
"""
import logging
import time
import traceback
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Document, DocumentPage, DocumentStatus, ProcessingHistory
from app.services import file_router
from app.services.exceptions import DocumentProcessingError

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    ok: bool
    page_count: int = 0
    failed_pages: list = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


@contextmanager
def stage(session: Session, document_id: uuid.UUID, name: str):
    """Record a pipeline stage in processing_history (committed immediately,
    so progress is visible while it runs). Yields a dict for extra `details`."""
    started = datetime.now(timezone.utc)
    row = ProcessingHistory(document_id=document_id, stage=name, status="started", started_at=started)
    session.add(row)
    session.commit()
    row_id = row.id
    t0 = time.perf_counter()
    details: dict = {}
    status = "failed"
    try:
        yield details
        status = "succeeded"
    except Exception as exc:
        session.rollback()
        details["error_type"] = type(exc).__name__
        details["error"] = str(exc)[:500]
        if isinstance(exc, DocumentProcessingError):
            details["error_code"] = exc.code
        else:
            details["traceback"] = traceback.format_exc()[-2000:]
        raise
    finally:
        entry = session.get(ProcessingHistory, row_id)
        entry.status = status
        entry.finished_at = datetime.now(timezone.utc)
        entry.duration_ms = int((time.perf_counter() - t0) * 1000)
        entry.details = details
        session.commit()


def process_document(session: Session, document_id: uuid.UUID) -> Outcome:
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(f"Document {document_id} does not exist")
    doc.status = DocumentStatus.processing
    doc.error_message = None
    session.commit()

    extraction = None
    try:
        with stage(session, document_id, "validate_file") as d:
            detection = file_router.detect(doc.stored_path, doc.original_filename)
            d.update(format=detection.format, kind=detection.kind.value)

        with stage(session, document_id, "extract_text") as d:
            processor = file_router.get_processor(detection.kind)
            extraction = processor.process(doc.stored_path)
            # Idempotent: a retry replaces earlier pages.
            session.execute(delete(DocumentPage).where(DocumentPage.document_id == document_id))
            total = 0
            for batch in extraction.batches:
                session.add_all(
                    DocumentPage(
                        document_id=document_id,
                        page_number=p.page_number,
                        extracted_text=p.text,
                        extraction_method=p.method,
                        ocr_confidence=p.ocr_confidence,
                    )
                    for p in batch
                )
                session.commit()  # bounded memory + visible progress
                total += len(batch)
            d.update(extraction.info)
            d["pages_saved"] = total

        doc = session.get(Document, document_id)
        doc.page_count = total
        doc.file_type = detection.format
        session.commit()  # status stays `processing`: later stages (extraction, validation) continue
        return Outcome(ok=True, page_count=total, failed_pages=extraction.info.get("failed_pages", []))

    except DocumentProcessingError as exc:
        _fail(session, document_id, exc.message)
        return Outcome(ok=False, error_code=exc.code, error_message=exc.message)
    except Exception:
        log.exception("Unexpected error processing document %s", document_id)
        msg = "An unexpected error occurred while processing this file."
        _fail(session, document_id, msg)
        return Outcome(ok=False, error_code="internal_error", error_message=msg)
    finally:
        if extraction is not None:
            extraction.close()


def _fail(session: Session, document_id: uuid.UUID, message: str) -> None:
    session.rollback()
    doc = session.get(Document, document_id)
    doc.status = DocumentStatus.failed
    doc.error_message = message
    session.commit()
