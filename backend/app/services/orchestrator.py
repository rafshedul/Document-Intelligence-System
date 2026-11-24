"""The whole chain for one document: process (incl. OCR) -> extract -> validate -> embed."""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core import db
from app.models import Document, DocumentStatus, ProcessingHistory
from app.services import embedding_service, extraction_service, pipeline, validation_service

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    ok: bool
    retryable: bool = False  # a temporary problem: the caller may try again after a delay
    error: str | None = None


def run_pipeline(document_id: uuid.UUID, attempt: int | None = None) -> RunResult:
    """Each step records its own failures on the document; this never raises.

    Safe to run twice for the same document: pages, extraction results of this attempt and embeddings
    are replaced, not added to.
    """
    try:
        with db.SessionLocal() as s:
            p = pipeline.process_document(s, document_id)
            if not p.ok:
                return RunResult(False, p.retryable, p.error_message)
            e = extraction_service.extract_document(s, document_id, version=attempt)
            if not e.ok:
                return RunResult(False, e.retryable, e.error_message)
            validation_service.validate_document(s, document_id)
            embedding_service.embed_document(s, document_id)
            doc = s.get(Document, document_id)
            if doc.status in (DocumentStatus.completed, DocumentStatus.needs_review):
                pipeline.mark_progress(s, document_id, "done", 100)
            return RunResult(True)
    except LookupError:  # deleted while queued
        log.info("Document %s no longer exists; nothing to do", document_id)
        return RunResult(False, False, "deleted")
    except Exception:
        log.exception("Pipeline crashed for %s", document_id)
        return RunResult(False, True, "unexpected error")


def record_event(document_id: uuid.UUID, stage: str, status: str, details: dict) -> None:
    """Add a row to processing_history and, for a retry wait, put the document back to queued."""
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        s.add(ProcessingHistory(document_id=document_id, stage=stage, status=status, started_at=now,
                                finished_at=now, duration_ms=0, details=details))
        doc = s.get(Document, document_id)
        if doc is not None and stage == "retry":
            doc.status, doc.stage = DocumentStatus.queued, "queued"
            doc.error_message = f"Temporary problem; retrying ({details['retry']} of {details['max_retries']})."
        s.commit()
