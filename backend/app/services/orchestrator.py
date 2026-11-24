"""The whole chain for one document: process -> extract -> validate."""
import logging
import uuid

from app.core import db
from app.services import extraction_service, pipeline, validation_service

log = logging.getLogger(__name__)


def run_pipeline(document_id: uuid.UUID) -> None:
    """Each step records its own failures on the document; this never raises."""
    try:
        with db.SessionLocal() as s:
            if not pipeline.process_document(s, document_id).ok:
                return
            if not extraction_service.extract_document(s, document_id).ok:
                return
            validation_service.validate_document(s, document_id)
    except Exception:
        log.exception("Pipeline crashed for %s", document_id)
