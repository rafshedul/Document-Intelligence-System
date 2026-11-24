"""Validate extracted invoices and decide the document's final status.

Each rule is a function returning a list of Finding(rule_code, severity, field,
message). Per-invoice rules are pure (they get an InvoiceView); the duplicate
and cross-document rules also need the database.

Money maths uses Decimal only.

Outcome (validate_document):
- any unresolved error  -> document `needs_review`
- only warnings         -> `completed`, `is_flagged = True`
- nothing               -> `completed`
"""
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models import Document, DocumentStatus, ExtractedResult, ValidationError as ValidationErrorRow
from app.models.enums import Severity
from app.services.field_verification import normalize
from app.services.pipeline import stage

log = logging.getLogger(__name__)

ERROR, WARNING = Severity.error, Severity.warning
SHEET_TYPES = {"xlsx", "xls", "csv"}
REQUIRED_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "total")
OPTIONAL_FIELDS = ("customer_name", "currency")
MONEY_FIELDS = ("subtotal", "tax", "total")
ZERO = Decimal(0)


@dataclass
class Finding:
    rule_code: str
    severity: Severity
    message: str
    field: str | None = None


@dataclass
class ItemView:
    position: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total: Decimal | None


@dataclass
class InvoiceView:
    """What the rules see: plain values, no ORM objects."""

    result_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    invoice_index: int = 0
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = None
    items: list[ItemView] = field(default_factory=list)
    confidence: dict[str, float] = field(default_factory=dict)  # scalar field -> confidence
    item_confidence: dict[int, float] = field(default_factory=dict)  # item position -> confidence
    raw_date_text: str | None = None  # what the LLM read, if the date could not be used

    @classmethod
    def from_result(cls, r: ExtractedResult) -> "InvoiceView":
        fc = r.field_confidence or {}
        parsed = (r.raw_llm_output or {}).get("parsed") or {}
        raw_date = (parsed.get("invoice_date") or {}).get("value")
        return cls(
            result_id=r.id, document_id=r.document_id, invoice_index=r.invoice_index,
            invoice_number=r.invoice_number, invoice_date=r.invoice_date, supplier_name=r.supplier_name,
            customer_name=r.customer_name, subtotal=r.subtotal, tax=r.tax, total=r.total, currency=r.currency,
            items=[ItemView(i.position, i.description, i.quantity, i.unit_price, i.line_total) for i in r.line_items],
            confidence={k: v["confidence"] for k, v in fc.items()
                        if k != "line_items" and isinstance(v, dict) and "confidence" in v},
            item_confidence={i["position"]: i["confidence"] for i in fc.get("line_items", [])},
            raw_date_text=raw_date,
        )


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= settings.validation_tolerance


# ---- per-invoice rules (pure) ---------------------------------------------------

def rule_missing_field(inv: InvoiceView) -> list[Finding]:
    """MISSING_FIELD: required fields are errors, customer/currency are warnings."""
    out = []
    for name in REQUIRED_FIELDS:
        if _blank(getattr(inv, name)):
            out.append(Finding("MISSING_FIELD", ERROR, f"Required field '{name}' is missing.", name))
    for name in OPTIONAL_FIELDS:
        if _blank(getattr(inv, name)):
            out.append(Finding("MISSING_FIELD", WARNING, f"Field '{name}' is missing.", name))
    return out


def rule_invalid_number(inv: InvoiceView) -> list[Finding]:
    """INVALID_NUMBER: money/quantity amounts that are not numbers, negative, or unusually large."""
    checks = [(n, getattr(inv, n)) for n in MONEY_FIELDS]
    for it in inv.items:
        checks += [(f"line_items[{it.position}].{n}", getattr(it, n)) for n in ("quantity", "unit_price", "line_total")]
    out = []
    for name, v in checks:
        if v is None:
            continue
        if not isinstance(v, Decimal) or not v.is_finite():
            out.append(Finding("INVALID_NUMBER", ERROR, f"'{name}' is not a valid number ({v!r}).", name))
        elif v < 0:
            out.append(Finding("INVALID_NUMBER", ERROR, f"'{name}' is negative ({v}).", name))
        elif v > settings.validation_max_amount:
            out.append(Finding("INVALID_NUMBER", ERROR,
                               f"'{name}' is unusually large ({v}; limit {settings.validation_max_amount}).", name))
    return out


def rule_line_item_mismatch(inv: InvoiceView) -> list[Finding]:
    """LINE_ITEM_MISMATCH: quantity x unit_price must equal line_total (within tolerance)."""
    out = []
    for it in inv.items:
        if None in (it.quantity, it.unit_price, it.line_total):
            continue  # cannot be checked
        expected = it.quantity * it.unit_price
        if not _close(expected, it.line_total):
            out.append(Finding(
                "LINE_ITEM_MISMATCH", ERROR,
                f"Line {it.position}: {it.quantity} x {it.unit_price} = {expected}, but line total is {it.line_total}.",
                f"line_items[{it.position}].line_total"))
    return out


def rule_subtotal_mismatch(inv: InvoiceView) -> list[Finding]:
    """SUBTOTAL_MISMATCH: sum of line totals must equal subtotal (only if every line has a total)."""
    if inv.subtotal is None or not inv.items or any(i.line_total is None for i in inv.items):
        return []
    total = sum((i.line_total for i in inv.items), ZERO)
    if _close(total, inv.subtotal):
        return []
    return [Finding("SUBTOTAL_MISMATCH", ERROR,
                    f"Line items add up to {total}, but the subtotal is {inv.subtotal}.", "subtotal")]


def rule_total_mismatch(inv: InvoiceView) -> list[Finding]:
    """TOTAL_MISMATCH: subtotal + tax must equal total (needs all three)."""
    if None in (inv.subtotal, inv.tax, inv.total):
        return []
    expected = inv.subtotal + inv.tax
    if _close(expected, inv.total):
        return []
    return [Finding("TOTAL_MISMATCH", ERROR,
                    f"Subtotal {inv.subtotal} + tax {inv.tax} = {expected}, but the total is {inv.total}.", "total")]


def rule_low_confidence(inv: InvoiceView) -> list[Finding]:
    """LOW_CONFIDENCE: values the extractor itself was unsure about (null values are MISSING_FIELD's job)."""
    limit = settings.validation_min_confidence
    out = []
    for name, conf in inv.confidence.items():
        if getattr(inv, name, None) is not None and conf < limit:
            out.append(Finding("LOW_CONFIDENCE", WARNING,
                               f"Confidence for '{name}' is {conf:.2f} (below {limit}).", name))
    for pos, conf in inv.item_confidence.items():
        if conf < limit and any(i.position == pos for i in inv.items):
            out.append(Finding("LOW_CONFIDENCE", WARNING,
                               f"Confidence for line item {pos} is {conf:.2f} (below {limit}).", f"line_items[{pos}]"))
    return out


def rule_invalid_date(inv: InvoiceView, today: date | None = None) -> list[Finding]:
    """INVALID_DATE: a date in the future, or text that could not be read as a date."""
    today = today or date.today()
    if inv.invoice_date is not None:
        if inv.invoice_date > today:
            return [Finding("INVALID_DATE", WARNING,
                            f"Invoice date {inv.invoice_date} is in the future.", "invoice_date")]
    elif not _blank(inv.raw_date_text):
        return [Finding("INVALID_DATE", WARNING,
                        f"The date {inv.raw_date_text!r} could not be parsed.", "invoice_date")]
    return []


PURE_RULES: list[Callable[[InvoiceView], list[Finding]]] = [
    rule_missing_field, rule_invalid_number, rule_line_item_mismatch, rule_subtotal_mismatch,
    rule_total_mismatch, rule_low_confidence, rule_invalid_date,
]


# ---- rules that look at other documents -------------------------------------------

def _norm_number(v: str | None) -> str | None:
    return v.strip().upper() if not _blank(v) else None


def _norm_name(v: str | None) -> str | None:
    if _blank(v):
        return None
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", normalize(v))).strip()


def _is_sheet(file_type: str | None) -> bool:
    return file_type in SHEET_TYPES


def _earlier_candidates(session: Session, doc: Document, inv: InvoiceView):
    """Latest-version results with the same invoice number that were uploaded earlier
    (or sit earlier in this same document). Returns [(ExtractedResult, Document)]."""
    number = _norm_number(inv.invoice_number)
    if number is None:
        return []
    latest = (select(ExtractedResult.document_id, func.max(ExtractedResult.version).label("v"))
              .group_by(ExtractedResult.document_id).subquery())
    rows = session.execute(
        select(ExtractedResult, Document)
        .join(Document, Document.id == ExtractedResult.document_id)
        .join(latest, and_(latest.c.document_id == ExtractedResult.document_id,
                           latest.c.v == ExtractedResult.version))
        .where(func.upper(func.trim(ExtractedResult.invoice_number)) == number)
    ).all()
    out = []
    for r, d in rows:
        if d.id == doc.id:
            if r.invoice_index < inv.invoice_index:
                out.append((r, d))
        elif (d.created_at, str(d.id)) < (doc.created_at, str(doc.id)):
            out.append((r, d))
    return out


def rule_duplicate_invoice(session: Session, doc: Document, inv: InvoiceView) -> list[Finding]:
    """DUPLICATE_INVOICE: same supplier + invoice number already exists.

    A PDF/image and a spreadsheet of the same invoice are *not* duplicates: that pairing is what
    CROSS_DOCUMENT is for. Two PDFs, or two spreadsheets, are.
    """
    supplier = _norm_name(inv.supplier_name)
    if supplier is None:
        return []
    out = []
    for r, d in _earlier_candidates(session, doc, inv):
        if _norm_name(r.supplier_name) != supplier:
            continue
        if d.id != doc.id and _is_sheet(d.file_type) != _is_sheet(doc.file_type):
            continue
        where = f"another invoice in this document (index {r.invoice_index})" if d.id == doc.id \
            else f"'{d.original_filename}'"
        out.append(Finding("DUPLICATE_INVOICE", ERROR,
                           f"Invoice {inv.invoice_number} from {inv.supplier_name} already exists in {where}.",
                           "invoice_number"))
    return out


def rule_duplicate_file(session: Session, doc: Document) -> list[Finding]:
    """DUPLICATE_FILE: another document has the same SHA-256.

    Note: documents.file_hash currently has a unique index, so the database already refuses a
    second copy; this rule only matters if that constraint is relaxed (e.g. to allow re-uploads).
    """
    others = session.scalars(select(Document).where(Document.file_hash == doc.file_hash, Document.id != doc.id)
                             ).all()
    return [Finding("DUPLICATE_FILE", ERROR,
                    f"This file is identical to '{o.original_filename}' (same SHA-256).", None) for o in others]


def rule_cross_document(session: Session, doc: Document, inv: InvoiceView) -> list[Finding]:
    """CROSS_DOCUMENT: the same invoice number in another PDF/image or spreadsheet must agree on
    total and supplier. Compared: PDF-PDF and PDF-spreadsheet (not spreadsheet-spreadsheet)."""
    out = []
    supplier = _norm_name(inv.supplier_name)
    for r, d in _earlier_candidates(session, doc, inv):
        if d.id == doc.id or (_is_sheet(doc.file_type) and _is_sheet(d.file_type)):
            continue
        if inv.total is not None and r.total is not None and not _close(inv.total, r.total):
            out.append(Finding("CROSS_DOCUMENT", ERROR,
                               f"Total {inv.total} differs from {r.total} in '{d.original_filename}' "
                               f"(same invoice number {inv.invoice_number}).", "total"))
        other_supplier = _norm_name(r.supplier_name)
        if supplier and other_supplier and supplier != other_supplier:
            out.append(Finding("CROSS_DOCUMENT", ERROR,
                               f"Supplier '{inv.supplier_name}' differs from '{r.supplier_name}' in "
                               f"'{d.original_filename}' (same invoice number {inv.invoice_number}).",
                               "supplier_name"))
    return out


# ---- orchestration --------------------------------------------------------------

OWNED_CODES = {"MISSING_FIELD", "INVALID_NUMBER", "LINE_ITEM_MISMATCH", "SUBTOTAL_MISMATCH", "TOTAL_MISMATCH",
               "DUPLICATE_INVOICE", "DUPLICATE_FILE", "LOW_CONFIDENCE", "INVALID_DATE", "CROSS_DOCUMENT"}


@dataclass
class ValidationOutcome:
    status: DocumentStatus | None
    errors: int = 0
    warnings: int = 0
    flagged: bool = False
    by_rule: dict = field(default_factory=dict)
    message: str | None = None


def validate_document(session: Session, document_id, today: date | None = None) -> ValidationOutcome:
    """Run every rule on the document's latest extraction, save findings, set the final status."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(f"Document {document_id} does not exist")
    if doc.status == DocumentStatus.failed:
        return ValidationOutcome(None, message="Document failed earlier; nothing to validate.")
    try:
        with stage(session, document_id, "validate") as d:
            outcome = _validate(session, doc, today)
            d.update(errors=outcome.errors, warnings=outcome.warnings, by_rule=outcome.by_rule,
                     status=outcome.status.value if outcome.status else None)
        return outcome
    except Exception:
        log.exception("Validation crashed for document %s", document_id)
        session.rollback()
        doc = session.get(Document, document_id)
        doc.status = DocumentStatus.needs_review
        doc.error_message = "Automatic validation could not be completed; please review manually."
        session.commit()
        return ValidationOutcome(DocumentStatus.needs_review, message=doc.error_message)


def _validate(session: Session, doc: Document, today: date | None) -> ValidationOutcome:
    latest = session.scalar(select(func.max(ExtractedResult.version)).where(ExtractedResult.document_id == doc.id))
    if latest is None:
        doc.status, doc.is_flagged = DocumentStatus.needs_review, False
        doc.error_message = "No extracted data to validate."
        session.commit()
        return ValidationOutcome(DocumentStatus.needs_review, message=doc.error_message)

    results = session.scalars(
        select(ExtractedResult).options(selectinload(ExtractedResult.line_items))
        .where(ExtractedResult.document_id == doc.id, ExtractedResult.version == latest)
        .order_by(ExtractedResult.invoice_index)).all()
    result_ids = [r.id for r in results]

    # Findings a person already resolved stay resolved; everything else is recomputed.
    resolved = {(x.result_id, x.rule_code, x.field) for x in session.scalars(
        select(ValidationErrorRow).where(ValidationErrorRow.document_id == doc.id,
                                         ValidationErrorRow.resolved.is_(True)))}
    session.execute(delete(ValidationErrorRow).where(
        ValidationErrorRow.document_id == doc.id, ValidationErrorRow.resolved.is_(False),
        ValidationErrorRow.rule_code.in_(OWNED_CODES),
        (ValidationErrorRow.result_id.in_(result_ids)) | (ValidationErrorRow.result_id.is_(None))))

    new: list[tuple[uuid.UUID | None, Finding]] = [(None, f) for f in rule_duplicate_file(session, doc)]
    for r in results:
        inv = InvoiceView.from_result(r)
        findings = [f for rule in PURE_RULES for f in
                    (rule_invalid_date(inv, today) if rule is rule_invalid_date else rule(inv))]
        findings += rule_duplicate_invoice(session, doc, inv)
        findings += rule_cross_document(session, doc, inv)
        new += [(r.id, f) for f in findings]

    by_rule: dict[str, int] = {}
    for rid, f in new:
        if (rid, f.rule_code, f.field) in resolved:
            continue
        by_rule[f.rule_code] = by_rule.get(f.rule_code, 0) + 1
        session.add(ValidationErrorRow(document_id=doc.id, result_id=rid, rule_code=f.rule_code,
                                       severity=f.severity, field=f.field, message=f.message))
    session.flush()

    # Status counts every unresolved finding on this extraction, including the EXTRACT_* ones.
    counts = dict(session.execute(
        select(ValidationErrorRow.severity, func.count()).where(
            ValidationErrorRow.document_id == doc.id, ValidationErrorRow.resolved.is_(False),
            (ValidationErrorRow.result_id.in_(result_ids)) | (ValidationErrorRow.result_id.is_(None)))
        .group_by(ValidationErrorRow.severity)).all())
    errors, warnings = counts.get(ERROR, 0), counts.get(WARNING, 0)
    doc.is_flagged = warnings > 0
    if errors:
        doc.status = DocumentStatus.needs_review
        doc.error_message = f"{errors} validation error(s) and {warnings} warning(s) need review."
    else:
        doc.status = DocumentStatus.completed
        doc.error_message = None
    session.commit()
    return ValidationOutcome(doc.status, errors, warnings, doc.is_flagged, by_rule)
