import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.documents import ERRORS, get_session
from app.core.security import require_auth
from app.models import DocumentStatus
from app.schemas.documents import ErrorResponse
from app.services import search_service as svc

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["search"], responses=ERRORS)


class SnippetOut(BaseModel):
    text: str
    highlights: list[list[int]]
    page: int | None


class InvoiceFields(BaseModel):
    invoice_index: int
    invoice_number: str | None
    invoice_date: str | None
    supplier_name: str | None
    customer_name: str | None
    subtotal: str | None
    tax: str | None
    total: str | None
    currency: str | None


class SearchHit(BaseModel):
    document_id: str
    filename: str
    status: str
    file_type: str
    score: float
    matched_by: list[str]
    keyword_rank: int | None
    semantic_rank: int | None
    snippet: SnippetOut | None
    invoice_count: int
    invoice: InvoiceFields | None


class SearchResponse(BaseModel):
    query: str
    mode: str
    items: list[SearchHit]
    total: int
    page: int
    page_size: int
    pages: int
    truncated: bool


@router.get("/search", response_model=SearchResponse, summary="Search documents (keyword, semantic or hybrid)",
            description="`keyword`: PostgreSQL full-text search; understands `\"exact phrase\"`, `OR` and `-excluded`. "
                        "`semantic`: nearest chunks by embedding. `hybrid`: both, merged with Reciprocal Rank "
                        "Fusion. One result per document with its best snippet (plain text + highlight offsets). "
                        "Filters apply to the document's latest extraction. At most 500 results are ranked.",
            responses={503: {"model": ErrorResponse}})
def search(q: str = Query(..., min_length=1, max_length=500, description="what to look for"),
           mode: Literal["keyword", "semantic", "hybrid"] = "hybrid",
           supplier: str | None = Query(None, max_length=200, description="contains, case-insensitive"),
           invoice_number: str | None = Query(None, max_length=128, description="exact, case-insensitive"),
           date_from: date | None = None, date_to: date | None = None,
           amount_min: Decimal | None = Query(None, description="invoice total, at least"),
           amount_max: Decimal | None = Query(None, description="invoice total, at most"),
           status: DocumentStatus | None = None, file_type: str | None = Query(None, max_length=10),
           page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=50),
           session: Session = Depends(get_session)):
    if not q.strip():
        raise HTTPException(422, "q must not be empty.")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to.")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(422, "amount_min must not be greater than amount_max.")
    filters = svc.SearchFilters(supplier=supplier, invoice_number=invoice_number, date_from=date_from, date_to=date_to,
                                amount_min=amount_min, amount_max=amount_max,
                                status=status.value if status else None, file_type=file_type)
    try:
        result = svc.search(session, q, mode, filters, page, page_size)
    except svc.SearchUnavailable as exc:
        raise HTTPException(503, {"message": str(exc), "code": "search_unavailable"})
    return {"query": q, "mode": mode, "items": result.items, "total": result.total, "page": result.page,
            "page_size": result.page_size, "pages": result.pages, "truncated": result.truncated}
