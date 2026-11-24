import uuid

from app.core.config import settings
from app.services.orchestrator import record_event, run_pipeline
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, name="process_document")
def process_document_task(self, document_id: str, attempt: int | None = None) -> str:
    """Run the full pipeline for one document.

    Idempotent: running it again for the same attempt replaces pages, results and embeddings.
    Temporary failures (LLM overloaded or unreachable, unexpected errors) are retried with exponential
    backoff, at most `task_max_retries` times; after that the document stays `failed`.
    Permanent ones (corrupted file, wrong password, bad API key) are not retried.
    """
    doc_id = uuid.UUID(document_id)
    result = run_pipeline(doc_id, attempt)
    if result.ok or not result.retryable:
        return document_id
    n = self.request.retries
    if n >= settings.task_max_retries:
        record_event(doc_id, "retry_exhausted", "failed", {"retries": n, "error": result.error})
        return document_id
    countdown = settings.task_retry_backoff_seconds * (2 ** n)
    record_event(doc_id, "retry", "scheduled",
                 {"retry": n + 1, "max_retries": settings.task_max_retries, "wait_seconds": countdown,
                  "error": result.error})
    raise self.retry(countdown=countdown, max_retries=settings.task_max_retries,
                     exc=RuntimeError(result.error or "retry"))
