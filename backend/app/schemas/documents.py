"""Request and response models (they also drive the OpenAPI docs)."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

EDITABLE_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                   "subtotal", "tax", "total", "currency")
ITEM_FIELDS = ("description", "quantity", "unit_price", "line_total")


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: Any | None = None


class ErrorResponse(BaseModel):
    """Every error from this API has this shape."""
    error: ErrorDetail


class UploadFileResult(BaseModel):
    filename: str
    status: str = Field(description="`queued` or `rejected`")
    id: str | None = None
    error: dict | None = Field(default=None, description="code, message, http_status when rejected")


class UploadResponse(BaseModel):
    accepted: int
    rejected: int
    files: list[UploadFileResult]


class DocumentListItem(BaseModel):
    id: str
    filename: str
    status: str
    stage: str | None
    progress: int
    flagged: bool
    file_type: str
    page_count: int | None
    created_at: str
    errors: int
    warnings: int


class DocumentList(BaseModel):
    items: list[DocumentListItem]
    total: int
    page: int
    page_size: int
    pages: int


class StatusResponse(BaseModel):
    id: str
    status: str
    stage: str | None = Field(description="current pipeline stage, `queued`, or `done`")
    progress: int = Field(description="0-100")
    error_message: str | None
    attempts: int = Field(description="how many times it has been queued (upload + reprocess)")
    updated_at: str


class ReprocessResponse(BaseModel):
    id: str
    status: str
    attempt: int


class FieldValue(BaseModel):
    value: str | None
    confidence: float | None = None
    source_snippet: str | None = None
    manual: bool = False


class LineItemOut(BaseModel):
    position: int
    description: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None


class InvoiceOut(BaseModel):
    id: str
    invoice_index: int
    pages: list[int | None]
    version: int
    source: str | None
    is_manually_corrected: bool
    fields: dict[str, FieldValue]
    line_items: list[LineItemOut]


class FindingOut(BaseModel):
    rule_code: str
    severity: str
    field: str | None
    message: str
    resolved: bool
    result_id: str | None = None


class HistoryOut(BaseModel):
    stage: str
    status: str
    started_at: str | None
    duration_ms: int | None
    details: dict | None


class DocumentSummary(BaseModel):
    id: str
    filename: str
    status: str
    stage: str | None
    progress: int
    flagged: bool
    file_type: str
    file_size: int
    page_count: int | None
    error_message: str | None
    created_at: str


class ResultResponse(BaseModel):
    document: DocumentSummary
    results: list[InvoiceOut]
    findings: list[FindingOut]
    history: list[HistoryOut]


class LineItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: int = Field(ge=1)
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    line_total: str | None = None


class CorrectionRequest(BaseModel):
    """Only the fields you send are changed. Send `null` or `""` to clear a value."""
    model_config = ConfigDict(extra="forbid")
    invoice_index: int = Field(default=0, ge=0, description="which invoice in the document")
    fields: dict[str, str | None] = Field(default_factory=dict,
                                          description=f"any of: {', '.join(EDITABLE_FIELDS)}")
    line_items: list[LineItemEdit] = Field(default_factory=list)
    corrected_by: str | None = Field(default=None, max_length=255)

    @field_validator("fields")
    @classmethod
    def _known_fields(cls, v):
        bad = sorted(set(v) - set(EDITABLE_FIELDS))
        if bad:
            raise ValueError(f"unknown field(s): {', '.join(bad)}")
        return v
