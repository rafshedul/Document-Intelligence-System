import uuid

from app.services.orchestrator import run_pipeline
from app.workers.celery_app import celery_app


@celery_app.task(name="process_document")
def process_document_task(document_id: str) -> str:
    """Run the full pipeline for one document. Safe to repeat: pages are replaced and
    extraction writes a new version."""
    run_pipeline(uuid.UUID(document_id))
    return document_id
