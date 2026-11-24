"""Turn document text into structured invoices and save them.

Flow: pages -> split into invoices (or map spreadsheet columns) -> LLM per
invoice (bounded concurrency, backoff) -> validate JSON with Pydantic (up to 2
re-asks) -> verify every value against its source snippet -> save to
extracted_results / line_items, with the raw LLM output kept.

Rules enforced in code, not just in the prompt:
- a value with no snippet, or whose snippet is not in the document text, is null;
- a value that is not inside its own snippet is null;
- invalid JSON after the retries leaves the document in `needs_review`.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    Document, DocumentPage, DocumentStatus, ExtractedResult, ExtractionMethod, LineItem,
    ValidationError as ValidationErrorRow,
)
from app.models.enums import Severity
from app.prompts import load_prompt
from app.services import spreadsheet_mapper
from app.services.extraction_schema import SCALAR_FIELDS, LLMExtraction
from app.services.field_verification import Verified, normalize, verify_decimal, verify_field
from app.services.invoice_segmenter import Segment, split_invoices
from app.services.llm_client import (
    CallStats, LLMClient, LLMError, LLMRefusalError, LLMUnavailableError, RateGate,
    call_with_backoff, get_llm_client,
)
from app.services.pipeline import stage

log = logging.getLogger(__name__)
PROMPT_NAME = "extraction_v1"
MAPPED_CONFIDENCE = 0.95  # value taken from a recognised column header, verified against the text


# ---- result containers --------------------------------------------------------

@dataclass
class Issue:
    rule_code: str
    severity: Severity
    message: str
    field: str | None = None


@dataclass
class ItemData:
    position: int
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    line_total: Decimal | None
    confidence: float
    source_snippet: str | None


@dataclass
class InvoiceData:
    invoice_index: int
    page_start: int | None
    page_end: int | None
    fields: dict[str, Verified] = field(default_factory=dict)
    items: list[ItemData] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    needs_review: bool = False  # LLM output unusable
    fatal: str | None = None  # LLM unavailable / rejected: abort the document
    fatal_retryable: bool = False  # ...and trying again later may work (outage, not a bad key)


@dataclass
class ExtractionOutcome:
    ok: bool
    invoices: int = 0
    needs_review: int = 0
    error_message: str | None = None
    retryable: bool = False  # the LLM was only temporarily unavailable, or an unexpected error


# ---- LLM extraction for one invoice -----------------------------------------------

def extract_segment(llm: LLMClient, text: str, index: int, page_start, page_end, gate: RateGate,
                    stats: CallStats) -> InvoiceData:
    data = InvoiceData(index, page_start, page_end)
    if len(text) > settings.extraction_max_segment_chars:
        data.needs_review = True
        data.issues.append(Issue("EXTRACT_SEGMENT_TOO_LARGE", Severity.error,
                                 f"Invoice text is {len(text):,} characters; the limit is "
                                 f"{settings.extraction_max_segment_chars:,}. Review it manually."))
        return data

    system, user_tpl = load_prompt(PROMPT_NAME)
    messages = [{"role": "user", "content": user_tpl.replace("{{DOCUMENT_TEXT}}", text)}]
    schema = LLMExtraction.model_json_schema()
    attempts: list[dict] = []
    parsed: LLMExtraction | None = None
    model = ""
    try:
        for attempt in range(1 + settings.llm_max_validation_retries):
            resp = call_with_backoff(
                lambda: llm.complete_json(system, messages, schema, temperature=0.0,
                                          max_tokens=settings.llm_max_output_tokens),
                gate=gate, stats=stats,
            )
            model = resp.model or model
            try:
                parsed = LLMExtraction.model_validate_json(resp.text)
                attempts.append({"output": resp.text, "error": None})
                break
            except (PydanticValidationError, ValueError) as exc:
                err = _short_error(exc, resp.truncated)
                attempts.append({"output": resp.text, "error": err})
                messages = messages + [
                    {"role": "assistant", "content": resp.text},
                    {"role": "user", "content": "That output is invalid: " + err +
                     " Return the corrected JSON object only."},
                ]
    except LLMRefusalError as exc:
        data.needs_review = True
        data.issues.append(Issue("EXTRACT_LLM_REFUSED", Severity.error, str(exc)))
        data.raw = {"prompt": PROMPT_NAME, "source": "llm", "attempts": attempts}
        return data
    except LLMUnavailableError as exc:
        data.fatal, data.fatal_retryable = str(exc), True
        return data
    except LLMError as exc:
        data.fatal = str(exc)
        return data

    data.raw = {"prompt": PROMPT_NAME, "source": "llm", "model": model, "attempts": attempts}
    if parsed is None:
        data.needs_review = True
        data.issues.append(Issue(
            "EXTRACT_LLM_OUTPUT_INVALID", Severity.error,
            f"The LLM did not return valid JSON after {len(attempts)} attempts: {attempts[-1]['error']}"))
        return data

    norm = normalize(text)
    for name in SCALAR_FIELDS:
        f = getattr(parsed, name)
        _put(data, name, verify_field(name, f.value, f.confidence, f.source_snippet, norm))
    _verify_items(data, parsed, norm)
    data.raw["parsed"] = parsed.model_dump()
    if parsed.multiple_invoices_detected:
        data.issues.append(Issue("EXTRACT_MULTIPLE_INVOICES", Severity.warning,
                                 "The text seems to contain more than one invoice; only the first was extracted."))
    return data


def _short_error(exc: Exception, truncated: bool) -> str:
    if truncated:
        return "The output was cut off at the length limit; return a more compact JSON object."
    if isinstance(exc, PydanticValidationError):
        parts = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8]]
        return "; ".join(parts) + "."
    return f"not valid JSON ({str(exc)[:150]})."


def _put(data: InvoiceData, name: str, v: Verified) -> None:
    data.fields[name] = v
    if v.rejected_reason:
        data.issues.append(Issue(v.rejected_reason, Severity.warning,
                                 f"Value for '{name}' was discarded (set to null).", name))


def _verify_items(data: InvoiceData, parsed: LLMExtraction, norm: str) -> None:
    from app.services.field_verification import snippet_in_text

    pos = 0
    for raw in parsed.line_items:
        if not snippet_in_text(raw.source_snippet, norm):
            data.issues.append(Issue("EXTRACT_LINE_ITEM_DROPPED", Severity.warning,
                                     f"A line item was dropped: its source snippet is not in the text "
                                     f"({(raw.description or '')[:60]!r})."))
            continue
        desc = raw.description
        if desc and normalize(desc) not in normalize(raw.source_snippet):
            data.issues.append(Issue("EXTRACT_VALUE_NOT_IN_SNIPPET", Severity.warning,
                                     "Line item description was discarded.", "line_items.description"))
            desc = None
        nums = {}
        for n in ("quantity", "unit_price", "line_total"):
            v, why = verify_decimal(getattr(raw, n), raw.source_snippet, norm)
            nums[n] = v
            if why:
                data.issues.append(Issue(why, Severity.warning, f"Line item {n} was discarded.", f"line_items.{n}"))
        pos += 1
        data.items.append(ItemData(pos, desc, nums["quantity"], nums["unit_price"], nums["line_total"],
                                   raw.confidence, raw.source_snippet.strip()))


# ---- spreadsheet path ----------------------------------------------------------------

def invoice_from_mapping(inv: spreadsheet_mapper.MappedInvoice, index: int, pages: list[int], norm: str) -> InvoiceData:
    data = InvoiceData(index, min(pages), max(pages))
    data.raw = {"source": "spreadsheet_mapping", "sheet": inv.sheet}
    for name in SCALAR_FIELDS:
        mf = inv.fields.get(name)
        if mf is None:
            data.fields[name] = Verified(None, 0.0, None)
        else:
            _put(data, name, verify_field(name, str(mf.value), MAPPED_CONFIDENCE, mf.snippet, norm))
    for pos, item in enumerate(inv.line_items, start=1):
        g = lambda k: item[k].value if k in item else None  # noqa: E731
        data.items.append(ItemData(pos, g("description"), g("quantity"), g("unit_price"), g("line_total"),
                                   MAPPED_CONFIDENCE, item["_row"].snippet))
    return data


# ---- orchestration ----------------------------------------------------------------------

def extract_document(session: Session, document_id, llm: LLMClient | None = None,
                     version: int | None = None) -> ExtractionOutcome:
    """Extract and save all invoices of a document. Never raises for LLM/data problems.

    `version` pins the result version (the pipeline passes the attempt number); running the same
    version again replaces its rows instead of adding duplicates. Without it, a new version is added."""
    doc = session.get(Document, document_id)
    if doc is None:
        raise LookupError(f"Document {document_id} does not exist")
    try:
        return _extract(session, doc, llm or get_llm_client(), version)
    except LLMUnavailableError as exc:  # retries inside the call ran out: worth trying again later
        return _mark(session, document_id, DocumentStatus.failed, str(exc), retryable=True)
    except LLMError as exc:  # fatal provider problem (bad key, bad request)
        return _mark(session, document_id, DocumentStatus.failed, str(exc))
    except Exception:
        log.exception("Unexpected error extracting document %s", document_id)
        return _mark(session, document_id, DocumentStatus.failed,
                     "An unexpected error occurred while extracting the invoice data.", retryable=True)


def _extract(session: Session, doc: Document, llm: LLMClient, pinned_version: int | None) -> ExtractionOutcome:
    pages = session.execute(
        select(DocumentPage.page_number, DocumentPage.extracted_text, DocumentPage.extraction_method)
        .where(DocumentPage.document_id == doc.id).order_by(DocumentPage.page_number)
    ).all()
    if not any((p.extracted_text or "").strip() for p in pages):
        return _mark(session, doc.id, DocumentStatus.needs_review, "No text was found in this document.")

    doc.status = DocumentStatus.processing
    session.commit()
    is_sheet = all(p.extraction_method == ExtractionMethod.spreadsheet for p in pages)
    page_texts = [(p.page_number, p.extracted_text or "") for p in pages]
    full_norm = normalize("\n".join(t for _, t in page_texts)) if is_sheet else ""

    with stage(session, doc.id, "segment") as d:
        mapped: list[InvoiceData] = []
        llm_jobs: list[Segment] = []
        if is_sheet:
            for sheet in spreadsheet_mapper.rebuild_sheets(page_texts):
                plan = spreadsheet_mapper.plan_sheet(sheet)
                if plan.invoices is None:
                    text = "\n".join(t for n, t in page_texts if n in set(sheet.pages))
                    llm_jobs.append(Segment(min(sheet.pages), max(sheet.pages), text, None, sheet.pages))
                    d.setdefault("sheets_to_llm", []).append({"sheet": sheet.name, "reason": plan.reason})
                else:
                    for inv in plan.invoices:
                        mapped.append(invoice_from_mapping(inv, 0, sheet.pages, full_norm))
                    d.setdefault("sheets_mapped", []).append(sheet.name)
        else:
            llm_jobs = split_invoices(page_texts)
        d.update(mapped_invoices=len(mapped), llm_segments=len(llm_jobs))

    stats, gate = CallStats(), RateGate()
    llm_results: dict[int, InvoiceData] = {}
    with stage(session, doc.id, "extract_fields") as d:
        with ThreadPoolExecutor(max_workers=max(1, settings.llm_concurrency)) as pool:
            futures = {
                pool.submit(extract_segment, llm, seg.text, i, seg.page_start, seg.page_end, gate, stats): i
                for i, seg in enumerate(llm_jobs)
            }
            for fut in as_completed(futures):
                res = fut.result()
                llm_results[futures[fut]] = res
                if res.fatal:  # stop paying for calls that will fail the same way
                    for other in futures:
                        other.cancel()
                    break
        d.update(llm_calls=stats.attempts, retries=stats.retries, rate_limited=stats.rate_limited,
                 input_tokens=stats.input_tokens, output_tokens=stats.output_tokens, model=settings.llm_model,
                 prompt=PROMPT_NAME)
        failed = next((r for r in llm_results.values() if r.fatal), None)
        if failed:
            raise (LLMUnavailableError if failed.fatal_retryable else LLMError)(failed.fatal)

    invoices = mapped + [llm_results[i] for i in sorted(llm_results)]
    for n, inv in enumerate(invoices):
        inv.invoice_index = n

    with stage(session, doc.id, "save_results") as d:
        if pinned_version is not None:
            version = pinned_version  # idempotent: redo of the same attempt replaces its own rows
            session.execute(delete(ExtractedResult).where(ExtractedResult.document_id == doc.id,
                                                          ExtractedResult.version == version))
        else:
            version = (session.scalar(select(func.max(ExtractedResult.version))
                                      .where(ExtractedResult.document_id == doc.id)) or 0) + 1
        for n, inv in enumerate(invoices, start=1):
            _save_invoice(session, doc.id, inv, version)
            if n % 100 == 0:
                session.commit()
        review = sum(1 for i in invoices if i.needs_review)
        doc = session.get(Document, doc.id)
        if review:
            doc.status = DocumentStatus.needs_review
            doc.error_message = f"{review} of {len(invoices)} invoice(s) could not be extracted automatically."
        session.commit()
        d.update(invoices=len(invoices), needs_review=review, version=version)
    return ExtractionOutcome(True, len(invoices), review, doc.error_message if review else None)


def _save_invoice(session: Session, document_id, inv: InvoiceData, version: int) -> None:
    f = {k: v.value for k, v in inv.fields.items()}
    result = ExtractedResult(
        document_id=document_id, invoice_index=inv.invoice_index, page_start=inv.page_start,
        page_end=inv.page_end, version=version,
        invoice_number=f.get("invoice_number"),
        invoice_date=_as_date(f.get("invoice_date")),
        supplier_name=f.get("supplier_name"), customer_name=f.get("customer_name"),
        subtotal=f.get("subtotal"), tax=f.get("tax"), total=f.get("total"), currency=f.get("currency"),
        field_confidence={
            **{k: {"confidence": v.confidence, "source_snippet": v.source_snippet} for k, v in inv.fields.items()},
            "line_items": [{"position": i.position, "confidence": i.confidence, "source_snippet": i.source_snippet}
                           for i in inv.items],
        },
        raw_llm_output=json.loads(json.dumps(inv.raw, default=str)),
    )
    result.line_items = [
        LineItem(position=i.position, description=i.description, quantity=i.quantity,
                 unit_price=i.unit_price, line_total=i.line_total) for i in inv.items
    ]
    session.add(result)
    session.flush()
    session.add_all(
        ValidationErrorRow(document_id=document_id, result_id=result.id, rule_code=i.rule_code,
                           severity=i.severity, field=i.field, message=i.message)
        for i in inv.issues
    )


def _as_date(v: Any):
    from datetime import date

    return v if isinstance(v, date) or v is None else date.fromisoformat(v)


def _mark(session: Session, document_id, status: DocumentStatus, message: str,
          retryable: bool = False) -> ExtractionOutcome:
    session.rollback()
    doc = session.get(Document, document_id)
    doc.status = status
    doc.error_message = message
    session.commit()
    return ExtractionOutcome(False, error_message=message, retryable=retryable)
