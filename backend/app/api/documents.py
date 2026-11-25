"""REST API for documents. Heavy work (OCR, extraction) never runs in a request: it goes to Celery."""
import hashlib
import logging
import math
import os
import uuid
from datetime import date, datetime
from urllib.parse import quote
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core import db
from app.core.config import settings
from app.core.files import safe_filename
from app.core.ratelimit import RateLimiter
from app.core.security import require_auth
from app.models import (
    Correction, Document, DocumentPage, DocumentStatus, ExtractedResult, ProcessingHistory, ValidationError as VErr,
)
from app.schemas.documents import (
    EDITABLE_FIELDS, CorrectionOut, CorrectionRequest, DocumentList, ErrorResponse, ReprocessResponse, ResultResponse,
    StatusResponse, UploadResponse,
)
from app.services import file_router, validation_service
from app.services.enqueue import QueueUnavailable, attempts_so_far, enqueue
from app.services.exceptions import DocumentProcessingError

log = logging.getLogger(__name__)
ERRORS = {401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["documents"], responses=ERRORS)
upload_limiter = RateLimiter(lambda: settings.upload_rate_limit_per_minute)
PAGE_TEXT_LIMIT = 4000
HTTP_FOR_CODE = {"unsupported_format": 415, "extension_mismatch": 415, "file_too_large": 413}


def get_session():
    with db.SessionLocal() as s:
        yield s


def _doc_or_404(session: Session, document_id: uuid.UUID) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found.")
    return doc


def _dec(v):
    return str(v) if isinstance(v, Decimal) else v


def _val(v):
    return v.isoformat() if hasattr(v, "isoformat") else _dec(v)


@router.get("/info", summary="Server settings the UI needs")
def info():
    return {"llm_provider": settings.llm_provider,
            "llm_model": "demo-rules" if settings.llm_provider == "demo" else settings.llm_model,
            "max_file_size_mb": settings.max_file_size_mb, "max_files_per_upload": settings.max_files_per_upload,
            "extensions": sorted(file_router.EXTENSION_FORMATS)}


# ---- 1. upload --------------------------------------------------------------------------------

def _rejected(name: str, status: int, code: str, message: str, existing: Document | None = None) -> dict:
    err = {"code": code, "message": message, "http_status": status}
    if existing is not None:
        err["existing_document_id"] = str(existing.id)
    return {"filename": name, "status": "rejected", "id": None, "error": err}


async def _save_upload(file: UploadFile, session: Session, background: BackgroundTasks) -> dict:
    name = safe_filename(file.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in file_router.EXTENSION_FORMATS:
        return _rejected(name, 415, "unsupported_format", f"'{ext or 'no extension'}' files are not supported. "
                         f"Supported: {', '.join(sorted(file_router.EXTENSION_FORMATS))}.")
    os.makedirs(settings.upload_dir, exist_ok=True)
    stored = os.path.join(settings.upload_dir, uuid.uuid4().hex)  # random name: nothing user-controlled
    h, size, limit = hashlib.sha256(), 0, settings.max_file_size_mb * 1024 * 1024
    try:
        with open(stored, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise _TooBig()
                h.update(chunk)
                out.write(chunk)
        try:  # reject mislabelled / empty / encrypted files now instead of queueing junk
            detection = file_router.detect(stored, name)
        except DocumentProcessingError as exc:
            os.remove(stored)
            return _rejected(name, HTTP_FOR_CODE.get(exc.code, 400), exc.code, exc.message)
        digest = h.hexdigest()
        existing = session.scalar(select(Document).where(Document.file_hash == digest))
        if existing:
            os.remove(stored)
            return _rejected(name, 409, "duplicate_file", f"This exact file was already uploaded as "
                             f"'{existing.original_filename}'.", existing)
        doc = Document(original_filename=name, stored_path=stored, file_type=detection.format, file_size=size,
                       file_hash=digest, status=DocumentStatus.queued)
        session.add(doc)
        session.commit()
    except _TooBig:
        os.remove(stored)
        return _rejected(name, 413, "file_too_large", f"The file is larger than {settings.max_file_size_mb} MB.")
    except BaseException:
        if os.path.exists(stored):
            os.remove(stored)
        raise
    try:
        enqueue(session, doc, background, "upload")
    except QueueUnavailable:
        return _rejected(name, 503, "queue_unavailable", "The processing queue is unavailable. Try again later.")
    return {"filename": name, "status": "queued", "id": str(doc.id), "error": None}


class _TooBig(Exception):
    pass


@router.post("/documents/upload", status_code=202, response_model=UploadResponse,
             summary="Upload one or more files",
             description="Saves each file, records it as `queued` and sends it to the background worker; returns "
                         "immediately with the document ids. OCR and AI run later, not in this request. Each file "
                         "succeeds or fails on its own (see `files[].error`). If every file is rejected the response "
                         "is the error of the first one, with all results under `error.details.files`.",
             responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
                        415: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
async def upload(request: Request, response: Response, background: BackgroundTasks,
                 files: list[UploadFile] = File(..., description="one or more files"),
                 session: Session = Depends(get_session)):
    if len(files) > settings.max_files_per_upload:
        raise HTTPException(400, f"At most {settings.max_files_per_upload} files per request.")
    wait = upload_limiter.check(request.client.host if request.client else "unknown", len(files))
    if wait is not None:
        raise HTTPException(429, "Too many uploads. Slow down.", headers={"Retry-After": str(math.ceil(wait))})
    results = [await _save_upload(f, session, background) for f in files]
    accepted = [r for r in results if r["status"] == "queued"]
    if not accepted:
        first = results[0]["error"]
        raise HTTPException(first["http_status"], {"message": first["message"], "code": first["code"], "files": results})
    return {"accepted": len(accepted), "rejected": len(results) - len(accepted), "files": results}


# ---- 2. list -----------------------------------------------------------------------------------

@router.get("/documents", response_model=DocumentList, summary="List documents (filter + pagination)")
def list_documents(status: DocumentStatus | None = None, file_type: str | None = Query(None, description="e.g. pdf, xlsx"),
                   flagged: bool | None = None, created_from: datetime | None = None, created_to: datetime | None = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                   session: Session = Depends(get_session)):
    q = select(Document)
    if status:
        q = q.where(Document.status == status)
    if file_type:
        q = q.where(Document.file_type == file_type.lower().lstrip("."))
    if flagged is not None:
        q = q.where(Document.is_flagged.is_(flagged))
    if created_from:
        q = q.where(Document.created_at >= created_from)
    if created_to:
        q = q.where(Document.created_at <= created_to)
    total = session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = session.scalars(q.order_by(Document.created_at.desc(), Document.id)
                           .offset((page - 1) * page_size).limit(page_size)).all()
    counts = {}
    if rows:
        counts = {(d, s.value): n for d, s, n in session.execute(
            select(VErr.document_id, VErr.severity, func.count())
            .where(VErr.document_id.in_([r.id for r in rows]), VErr.resolved.is_(False))
            .group_by(VErr.document_id, VErr.severity)).all()}
    return {"items": [{"id": str(d.id), "filename": d.original_filename, "status": d.status.value, "stage": d.stage,
                       "progress": d.progress, "flagged": d.is_flagged, "file_type": d.file_type,
                       "page_count": d.page_count, "created_at": d.created_at.isoformat(),
                       "errors": counts.get((d.id, "error"), 0), "warnings": counts.get((d.id, "warning"), 0)}
                      for d in rows],
            "total": total, "page": page, "page_size": page_size, "pages": math.ceil(total / page_size) if total else 0}


# ---- 3. status ----------------------------------------------------------------------------------

@router.get("/documents/{document_id}/status", response_model=StatusResponse, summary="Where is it in the pipeline?")
def get_status(document_id: uuid.UUID, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    return {"id": str(d.id), "status": d.status.value, "stage": d.stage, "progress": d.progress,
            "error_message": d.error_message, "attempts": attempts_so_far(session, d.id),
            "updated_at": d.updated_at.isoformat()}


# ---- 4. result ----------------------------------------------------------------------------------

def build_result(session: Session, d: Document) -> dict:
    latest = session.scalar(select(func.max(ExtractedResult.version)).where(ExtractedResult.document_id == d.id))
    results = session.scalars(
        select(ExtractedResult).options(selectinload(ExtractedResult.line_items))
        .where(ExtractedResult.document_id == d.id, ExtractedResult.version == latest)
        .order_by(ExtractedResult.invoice_index)).all() if latest else []
    findings = session.scalars(select(VErr).where(VErr.document_id == d.id)
                               .order_by(VErr.severity, VErr.rule_code)).all()
    history = session.scalars(select(ProcessingHistory).where(ProcessingHistory.document_id == d.id)
                              .order_by(ProcessingHistory.started_at)).all()
    return {
        "document": {"id": str(d.id), "filename": d.original_filename, "status": d.status.value, "stage": d.stage,
                     "progress": d.progress, "flagged": d.is_flagged, "file_type": d.file_type,
                     "file_size": d.file_size, "page_count": d.page_count, "error_message": d.error_message,
                     "created_at": d.created_at.isoformat()},
        "results": [{
            "id": str(r.id), "invoice_index": r.invoice_index, "pages": [r.page_start, r.page_end],
            "version": r.version, "source": (r.raw_llm_output or {}).get("source"),
            "is_manually_corrected": r.is_manually_corrected,
            "fields": {n: {"value": _val(getattr(r, n)), **{k: v for k, v in ((r.field_confidence or {}).get(n) or {}).items()
                                                            if k in ("confidence", "source_snippet", "manual")}}
                       for n in EDITABLE_FIELDS},
            "line_items": [{"position": i.position, "description": i.description, "quantity": _dec(i.quantity),
                            "unit_price": _dec(i.unit_price), "line_total": _dec(i.line_total)}
                           for i in r.line_items],
        } for r in results],
        "findings": [{"rule_code": f.rule_code, "severity": f.severity.value, "field": f.field, "message": f.message,
                      "resolved": f.resolved, "result_id": str(f.result_id) if f.result_id else None}
                     for f in findings],
        "history": [{"stage": h.stage, "status": h.status, "started_at": h.started_at.isoformat() if h.started_at else None,
                     "duration_ms": h.duration_ms, "details": h.details} for h in history],
    }


@router.get("/documents/{document_id}/result", response_model=ResultResponse,
            summary="Structured data, validation errors and processing history")
def get_result(document_id: uuid.UUID, session: Session = Depends(get_session)):
    return build_result(session, _doc_or_404(session, document_id))


@router.get("/documents/{document_id}", summary="Result plus extracted page text (used by the web page)")
def get_document(document_id: uuid.UUID, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    out = build_result(session, d)
    pages = session.scalars(select(DocumentPage).where(DocumentPage.document_id == d.id)
                            .order_by(DocumentPage.page_number).limit(50)).all()
    out["pages"] = [{"page": p.page_number, "method": p.extraction_method.value, "ocr_confidence": _dec(p.ocr_confidence),
                     "text": (p.extracted_text or "")[:PAGE_TEXT_LIMIT]} for p in pages]
    return out


# ---- the original file, and the correction log ---------------------------------------------------------------

MEDIA_TYPES = {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png", "tiff": "image/tiff",
               "csv": "text/csv", "xls": "application/vnd.ms-excel",
               "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
BROWSER_PREVIEW = {"pdf", "jpeg", "png"}  # what a browser can show; everything else is offered as a download


@router.get("/documents/{document_id}/file", summary="The original uploaded file",
            description="For the preview next to the extracted data. The type comes from what the file was detected "
                        "as at upload, never from the file's own claims, and the response carries a CSP that forbids "
                        "scripts, so an uploaded file cannot run anything. Only PDF and JPG/PNG are shown inline.",
            response_class=FileResponse, responses={404: {"model": ErrorResponse}})
def get_file(document_id: uuid.UUID, download: bool = False, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    if not os.path.isfile(d.stored_path):
        raise HTTPException(404, "The stored file is missing.")
    inline = d.file_type in BROWSER_PREVIEW and not download
    return FileResponse(
        d.stored_path, media_type=MEDIA_TYPES.get(d.file_type, "application/octet-stream"),
        headers={"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(d.original_filename)}",
                 "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "private, no-store"})


@router.get("/documents/{document_id}/corrections", response_model=list[CorrectionOut],
            summary="Who changed what: the manual correction log, newest first")
def get_corrections(document_id: uuid.UUID, session: Session = Depends(get_session)):
    _doc_or_404(session, document_id)
    rows = session.execute(
        select(Correction, ExtractedResult.invoice_index, ExtractedResult.version)
        .join(ExtractedResult, ExtractedResult.id == Correction.result_id)
        .where(ExtractedResult.document_id == document_id)
        .order_by(Correction.corrected_at.desc(), Correction.id)).all()
    return [{"id": str(c.id), "invoice_index": idx, "version": ver, "field": c.field, "old_value": c.old_value,
             "new_value": c.new_value, "corrected_by": c.corrected_by, "corrected_at": c.corrected_at.isoformat()}
            for c, idx, ver in rows]


# ---- 5. manual correction ----------------------------------------------------------------------

MAX_LEN = {"invoice_number": 128, "supplier_name": 512, "customer_name": 512}
MONEY_FIELDS = ("subtotal", "tax", "total", "unit_price", "line_total")


def _coerce(name: str, raw: str | None):
    """Turn user text into the stored type, or raise ValueError with a message for the user."""
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    base = name.split(".")[-1]
    if base == "invoice_date":
        if len(raw) != 10:
            raise ValueError("use the format YYYY-MM-DD")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ValueError("not a real date (use YYYY-MM-DD)")
    if base in MONEY_FIELDS or base == "quantity":
        try:
            d = Decimal(raw)
        except InvalidOperation:
            raise ValueError("not a number")
        places = 3 if base == "quantity" else 2
        if not d.is_finite() or abs(d) >= Decimal(10) ** 11:
            raise ValueError("not a usable number")
        if d.as_tuple().exponent < -places:
            raise ValueError(f"at most {places} decimal places")
        return d
    if base == "currency":
        if not (len(raw) == 3 and raw.isalpha() and raw.isascii()):
            raise ValueError("use a 3-letter ISO code such as USD")
        return raw.upper()
    limit = MAX_LEN.get(base, 2000)
    if len(raw) > limit:
        raise ValueError(f"at most {limit} characters")
    return raw


def _text(v) -> str | None:
    return None if v is None else str(v.isoformat() if hasattr(v, "isoformat") else v)


@router.patch("/documents/{document_id}/result", response_model=ResultResponse,
              summary="Correct extracted values by hand",
              description="Changes only the fields you send. Every change is written to the `corrections` table "
                          "(old value, new value, who, when), then validation runs again and the document's status "
                          "is updated. Not allowed while the document is queued or processing.",
              responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
def correct_result(document_id: uuid.UUID, body: CorrectionRequest, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    if d.status in (DocumentStatus.queued, DocumentStatus.processing):
        raise HTTPException(409, "The document is still being processed. Try again when it has finished.")
    latest = session.scalar(select(func.max(ExtractedResult.version)).where(ExtractedResult.document_id == d.id))
    r = session.scalars(select(ExtractedResult).options(selectinload(ExtractedResult.line_items)).where(
        ExtractedResult.document_id == d.id, ExtractedResult.version == latest,
        ExtractedResult.invoice_index == body.invoice_index).with_for_update()).first() if latest else None
    if r is None:
        raise HTTPException(404, "No extracted result to correct for this invoice.")

    planned, problems = [], []  # (label, setter, old, new)
    for name, raw in body.fields.items():
        try:
            planned.append((name, ("result", name), getattr(r, name), _coerce(name, raw)))
        except ValueError as exc:
            problems.append({"field": f"fields.{name}", "message": str(exc)})
    items = {i.position: i for i in r.line_items}
    for edit in body.line_items:
        item = items.get(edit.position)
        if item is None:
            problems.append({"field": f"line_items.{edit.position}", "message": "no line item at this position"})
            continue
        for fname in edit.model_fields_set - {"position"}:
            try:
                planned.append((f"line_items[{edit.position}].{fname}", ("item", item, fname),
                                getattr(item, fname), _coerce(fname, getattr(edit, fname))))
            except ValueError as exc:
                problems.append({"field": f"line_items.{edit.position}.{fname}", "message": str(exc)})
    if problems:
        raise HTTPException(422, {"message": "Some values are not valid.", "problems": problems})

    changed = [p for p in planned if p[2] != p[3]]
    if not changed:
        raise HTTPException(400, "No changes: the values you sent are the ones already stored.")
    confidence = dict(r.field_confidence or {})
    for label, target, old, new in changed:
        session.add(Correction(result_id=r.id, field=label, old_value=_text(old), new_value=_text(new),
                               corrected_by=body.corrected_by or "api"))
        if target[0] == "result":
            setattr(r, target[1], new)
            confidence[target[1]] = {"confidence": 1.0, "source_snippet": None, "manual": True}
        else:
            setattr(target[1], target[2], new)
    r.field_confidence = confidence
    r.is_manually_corrected = True
    session.commit()
    validation_service.validate_document(session, d.id)
    session.expire_all()
    return build_result(session, session.get(Document, d.id))


# ---- 6. reprocess / 7. delete --------------------------------------------------------------------

@router.post("/documents/{document_id}/reprocess", status_code=202, response_model=ReprocessResponse,
             summary="Run a failed or needs_review document through the pipeline again",
             responses={409: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
def reprocess(document_id: uuid.UUID, background: BackgroundTasks, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    if d.status not in (DocumentStatus.failed, DocumentStatus.needs_review):
        raise HTTPException(409, f"Only failed or needs_review documents can be reprocessed (this one is {d.status.value}).")
    try:
        attempt = enqueue(session, d, background, "reprocess")
    except QueueUnavailable:
        raise HTTPException(503, "The processing queue is unavailable. Try again later.")
    return {"id": str(d.id), "status": "queued", "attempt": attempt}


@router.delete("/documents/{document_id}", status_code=204, summary="Delete a document and everything derived from it",
               responses={409: {"model": ErrorResponse}})
def delete_document(document_id: uuid.UUID, session: Session = Depends(get_session)):
    d = _doc_or_404(session, document_id)
    if d.status == DocumentStatus.processing:
        raise HTTPException(409, "The document is being processed right now. Try again in a moment.")
    path = d.stored_path
    session.delete(d)  # pages, results, line items, findings, history, corrections, embeddings cascade
    session.commit()
    try:
        os.remove(path)
    except OSError:
        log.warning("Could not remove stored file for %s", document_id)
    return Response(status_code=204)
