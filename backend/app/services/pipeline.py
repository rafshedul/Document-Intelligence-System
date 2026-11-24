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

from sqlalchemy import delete, update
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
    retryable: bool = False  # a temporary problem: trying again later may work


# (start %, end %) of the overall progress bar for each stage
STAGE_PROGRESS = {
    "validate_file": (0, 5), "extract_text": (5, 50), "segment": (50, 52), "extract_fields": (52, 80),
    "save_results": (80, 85), "validate": (85, 95), "embed": (95, 100),
}


def mark_progress(session: Session, document_id: uuid.UUID, stage: str | None, pct: int) -> None:
    """Record where the document is (`stage`) and how far along (0-100). Committed at once so the
    status endpoint sees it while the stage is still running."""
    session.execute(update(Document).where(Document.id == document_id)
                    .values(stage=stage, progress=max(0, min(100, int(pct)))))
    session.commit()


@contextmanager
def stage(session: Session, document_id: uuid.UUID, name: str):
    """Record a pipeline stage in processing_history (committed immediately,
    so progress is visible while it runs). Yields a dict for extra `details`."""
    started = datetime.now(timezone.utc)
    row = ProcessingHistory(document_id=document_id, stage=name, status="started", started_at=started)
    session.add(row)
    session.commit()
    row_id = row.id
    lo_hi = STAGE_PROGRESS.get(name)
    if lo_hi:
        mark_progress(session, document_id, name, lo_hi[0])
    t0 = time.perf_counter()
    details: dict = {}
    status = "failed"
    try:
        yield details
        status = "succeeded"
        if lo_hi:
            mark_progress(session, document_id, name, lo_hi[1])
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
            lo, hi = STAGE_PROGRESS["extract_text"]
            pages_known = extraction.info.get("page_count") if detection.kind.value in ("pdf", "image") else None
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
                if pages_known:  # progress by page, so a 1000-page PDF moves while it runs
                    mark_progress(session, document_id, "extract_text", lo + (hi - lo) * total / pages_known)
            d.update(extraction.info)
            d["pages_saved"] = total

        doc = session.get(Document, document_id)
        doc.page_count = total
        doc.file_type = detection.format
        session.commit()  # status stays `processing`: later stages (extraction, validation) continue
        return Outcome(ok=True, page_count=total, failed_pages=extraction.info.get("failed_pages", []))

    except DocumentProcessingError as exc:
        _fail(session, document_id, exc.message)
        return Outcome(ok=False, error_code=exc.code, error_message=exc.message, retryable=False)
    except Exception:
        log.exception("Unexpected error processing document %s", document_id)
        msg = "An unexpected error occurred while processing this file."
        _fail(session, document_id, msg)
        return Outcome(ok=False, error_code="internal_error", error_message=msg, retryable=True)
    finally:
        if extraction is not None:
            extraction.close()


def _fail(session: Session, document_id: uuid.UUID, message: str) -> None:
    session.rollback()
    doc = session.get(Document, document_id)
    doc.status = DocumentStatus.failed
    doc.error_message = message
    session.commit()
