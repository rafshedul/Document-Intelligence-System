"""Upload + inspect documents. Processing runs in a background task (no Redis needed yet)."""
import hashlib
import logging
import os
import uuid
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core import db
from app.core.config import settings
from app.models import (
    Document, DocumentPage, DocumentStatus, ExtractedResult, ProcessingHistory, ValidationError as VErr,
)
from app.services import extraction_service, file_router, pipeline, validation_service

router = APIRouter(prefix="/api")
log = logging.getLogger(__name__)
PAGE_TEXT_LIMIT = 4000


def get_session():
    with db.SessionLocal() as s:
        yield s


def run_pipeline(document_id: uuid.UUID) -> None:
    """process -> extract -> validate. Each step records its own failures; nothing raises."""
    try:
        with db.SessionLocal() as s:
            if not pipeline.process_document(s, document_id).ok:
                return
            if not extraction_service.extract_document(s, document_id).ok:
                return
            validation_service.validate_document(s, document_id)
    except Exception:
        log.exception("Pipeline crashed for %s", document_id)


@router.get("/info")
def info():
    return {"llm_provider": settings.llm_provider, "llm_model": "demo-rules" if settings.llm_provider == "demo"
            else settings.llm_model, "max_file_size_mb": settings.max_file_size_mb,
            "extensions": sorted(file_router.EXTENSION_FORMATS)}


@router.post("/documents", status_code=202)
async def upload(background: BackgroundTasks, file: UploadFile = File(...), session: Session = Depends(get_session)):
    name = file.filename or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext not in file_router.EXTENSION_FORMATS:
        raise HTTPException(400, f"'{ext or 'no extension'}' files are not supported. "
                                 f"Supported: {', '.join(sorted(file_router.EXTENSION_FORMATS))}")
    os.makedirs(settings.upload_dir, exist_ok=True)
    stored = os.path.join(settings.upload_dir, uuid.uuid4().hex)
    h, size, limit = hashlib.sha256(), 0, settings.max_file_size_mb * 1024 * 1024
    try:
        with open(stored, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File is larger than {settings.max_file_size_mb} MB.")
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        if os.path.exists(stored):
            os.remove(stored)
        raise
    digest = h.hexdigest()
    existing = session.scalar(select(Document).where(Document.file_hash == digest))
    if existing:
        os.remove(stored)
        raise HTTPException(409, detail={"message": "This exact file was already uploaded.",
                                         "document_id": str(existing.id), "filename": existing.original_filename})
    doc = Document(original_filename=name, stored_path=stored, file_type=ext.lstrip("."), file_size=size,
                   file_hash=digest, status=DocumentStatus.queued)
    session.add(doc)
    session.commit()
    background.add_task(run_pipeline, doc.id)
    return {"id": str(doc.id), "status": doc.status.value}


FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency")


def _dec(v):
    return str(v) if isinstance(v, Decimal) else v


def _val(v):
    return v.isoformat() if hasattr(v, "isoformat") else _dec(v)


@router.get("/documents")
def list_documents(session: Session = Depends(get_session)):
    rows = session.execute(select(Document).order_by(Document.created_at.desc()).limit(200)).scalars().all()
    errs = dict(((d, s.value), n) for d, s, n in session.execute(
        select(VErr.document_id, VErr.severity, func.count()).where(VErr.resolved.is_(False))
        .group_by(VErr.document_id, VErr.severity)).all())
    return [{"id": str(d.id), "filename": d.original_filename, "status": d.status.value, "flagged": d.is_flagged,
             "file_type": d.file_type, "page_count": d.page_count, "created_at": d.created_at.isoformat(),
             "errors": errs.get((d.id, "error"), 0), "warnings": errs.get((d.id, "warning"), 0)} for d in rows]


@router.get("/documents/{document_id}")
def get_document(document_id: uuid.UUID, session: Session = Depends(get_session)):
    d = session.get(Document, document_id)
    if d is None:
        raise HTTPException(404, "Document not found")
    pages = session.scalars(select(DocumentPage).where(DocumentPage.document_id == d.id)
                            .order_by(DocumentPage.page_number).limit(50)).all()
    history = session.scalars(select(ProcessingHistory).where(ProcessingHistory.document_id == d.id)
                              .order_by(ProcessingHistory.started_at)).all()
    latest = session.scalar(select(func.max(ExtractedResult.version)).where(ExtractedResult.document_id == d.id))
    results = session.scalars(
        select(ExtractedResult).options(selectinload(ExtractedResult.line_items))
        .where(ExtractedResult.document_id == d.id, ExtractedResult.version == latest)
        .order_by(ExtractedResult.invoice_index)).all() if latest else []
    findings = session.scalars(select(VErr).where(VErr.document_id == d.id).order_by(VErr.severity, VErr.rule_code)).all()
    return {
        "id": str(d.id), "filename": d.original_filename, "status": d.status.value, "flagged": d.is_flagged,
        "file_type": d.file_type, "file_size": d.file_size, "page_count": d.page_count,
        "error_message": d.error_message, "created_at": d.created_at.isoformat(),
        "history": [{"stage": h.stage, "status": h.status, "duration_ms": h.duration_ms, "details": h.details}
                    for h in history],
        "pages": [{"page": p.page_number, "method": p.extraction_method.value,
                   "ocr_confidence": _dec(p.ocr_confidence), "text": (p.extracted_text or "")[:PAGE_TEXT_LIMIT]}
                  for p in pages],
        "results": [{
            "id": str(r.id), "invoice_index": r.invoice_index, "pages": [r.page_start, r.page_end],
            "fields": {n: {"value": _val(getattr(r, n)), **((r.field_confidence or {}).get(n) or {})}
                       for n in FIELDS},
            "line_items": [{"position": i.position, "description": i.description, "quantity": _dec(i.quantity),
                            "unit_price": _dec(i.unit_price), "line_total": _dec(i.line_total)}
                           for i in r.line_items],
            "source": (r.raw_llm_output or {}).get("source"),
        } for r in results],
        "findings": [{"rule_code": f.rule_code, "severity": f.severity.value, "field": f.field, "message": f.message,
                      "resolved": f.resolved} for f in findings],
    }
