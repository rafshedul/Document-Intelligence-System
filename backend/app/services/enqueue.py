"""Put a document on the queue (upload and reprocess both use this)."""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document, DocumentStatus, ProcessingHistory
from app.services.orchestrator import run_pipeline

log = logging.getLogger(__name__)


class QueueUnavailable(Exception):
    pass


def attempts_so_far(session: Session, document_id: uuid.UUID) -> int:
    return session.scalar(select(func.count()).select_from(ProcessingHistory).where(
        ProcessingHistory.document_id == document_id, ProcessingHistory.stage == "enqueue")) or 0


def enqueue(session: Session, doc: Document, background: BackgroundTasks, reason: str) -> int:
    """Mark the document queued, log the attempt number in processing_history, and send the job.

    Returns the attempt number. Raises QueueUnavailable (and marks the document failed) if the broker
    cannot be reached.
    """
    attempt = attempts_so_far(session, doc.id) + 1
    now = datetime.now(timezone.utc)
    session.add(ProcessingHistory(document_id=doc.id, stage="enqueue", status="succeeded", started_at=now,
                                  finished_at=now, duration_ms=0, details={"attempt": attempt, "reason": reason}))
    doc.status, doc.stage, doc.progress, doc.error_message = DocumentStatus.queued, "queued", 0, None
    session.commit()
    if settings.task_backend == "celery":
        from app.workers.tasks import process_document_task

        try:
            process_document_task.delay(str(doc.id), attempt)
        except Exception as exc:  # broker down: don't leave the document queued forever
            log.exception("Could not enqueue %s", doc.id)
            doc.status, doc.error_message = DocumentStatus.failed, "The processing queue is unavailable. Try again later."
            session.commit()
            raise QueueUnavailable() from exc
    else:
        background.add_task(run_pipeline, doc.id, attempt)
    return attempt
