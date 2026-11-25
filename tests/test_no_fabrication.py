"""The system must never invent data. A hostile mock LLM tries to; the checks must stop it. Needs TEST_DATABASE_URL for the end-to-end ones."""
import hashlib
import os
import uuid

import pytest
import sqlalchemy as sa
from mocks import MockLLM, field, response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document, DocumentPage, DocumentStatus, ExtractedResult, ExtractionMethod, ValidationError as VErr
from app.services import extraction_service as es
from app.services import validation_service
from app.services.demo_llm import extract_demo
from app.services.llm_client import CallStats, RateGate

NOT_AN_INVOICE = "Dear team,\nThe meeting is moved to Thursday at 10:00.\nPlease bring your laptops.\nRegards,\nSam\n"
SCALARS = ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency")


def hallucination() -> dict:
    """What a model that makes things up would say: a complete, plausible, well-formed invoice, with confident
    confidences and 'source snippets' that look real but are not in the text."""
    return response(
        invoice_number=field("INV-2026-0099", "Invoice No: INV-2026-0099", 0.99),
        invoice_date=field("2026-03-05", "Date: 2026-03-05", 0.99),
        supplier_name=field("Acme Corp", "Supplier: Acme Corp", 0.99),
        customer_name=field("Bob Ltd", "Bill to: Bob Ltd", 0.99),
        subtotal=field("100.00", "Subtotal: 100.00", 0.99),
        tax=field("10.00", "Tax: 10.00", 0.99),
        total=field("110.00", "Total: 110.00 USD", 0.99),
        currency=field("USD", "Total: 110.00 USD", 0.99),
        line_items=[{"description": "Consulting", "quantity": "1", "unit_price": "100.00", "line_total": "100.00",
                     "confidence": 0.99, "source_snippet": "Consulting 1 x 100.00 = 100.00"}],
    )


def extract(llm, text):
    return es.extract_segment(llm, text, 0, 1, 1, RateGate(), CallStats())


def test_a_model_that_invents_everything_gets_nothing_through():
    r = extract(MockLLM([hallucination()]), NOT_AN_INVOICE)
    assert {n: r.fields[n].value for n in SCALARS} == {n: None for n in SCALARS}
    assert all(r.fields[n].confidence == 0.0 and r.fields[n].source_snippet is None for n in SCALARS)
    assert r.items == []
    assert not r.needs_review and not r.fatal
    assert {i.rule_code for i in r.issues} == {"EXTRACT_SNIPPET_NOT_FOUND", "EXTRACT_LINE_ITEM_DROPPED"}  # each lie is logged


def test_an_honest_model_returns_nulls_and_that_is_not_an_error():
    r = extract(MockLLM([response()]), NOT_AN_INVOICE)
    assert all(r.fields[n].value is None for n in SCALARS) and r.items == [] and r.issues == [] and not r.needs_review


def test_only_what_the_text_supports_survives():
    text = "Invoice No: INV-7\nTotal: 55.00 EUR\nThanks for your business\n"
    r = extract(MockLLM([hallucination() | {  # the lie, plus two true values
        "invoice_number": field("INV-7", "Invoice No: INV-7"), "total": field("55.00", "Total: 55.00 EUR"),
        "currency": field("EUR", "Total: 55.00 EUR")}]), text)
    assert {n: r.fields[n].value for n in SCALARS if r.fields[n].value is not None}.keys() == {"invoice_number", "total", "currency"}
    assert r.fields["supplier_name"].value is None and r.fields["invoice_date"].value is None and r.items == []


@pytest.mark.parametrize("lie", ["110.00", "1100.00", "0.01", "55.01", "-55.00", "5.50"])
def test_a_wrong_amount_is_rejected_even_when_the_snippet_is_real(lie):
    text = "Invoice No: INV-7\nTotal: 55.00 EUR\n"
    r = extract(MockLLM([response(total=field(lie, "Total: 55.00 EUR"))]), text)
    assert r.fields["total"].value is None
    assert [i.rule_code for i in r.issues] == ["EXTRACT_VALUE_NOT_IN_SNIPPET"]


def test_a_real_snippet_cannot_vouch_for_a_different_value():
    text = "Supplier: Acme Ltd\nCustomer: Rahim Traders\n"
    r = extract(MockLLM([response(supplier_name=field("Globex Inc", "Supplier: Acme Ltd"))]), text)
    assert r.fields["supplier_name"].value is None


def test_a_value_with_no_snippet_is_dropped_however_confident():
    r = extract(MockLLM([response(invoice_number=field("INV-1", None, 1.0))]), "Invoice No: INV-1\n")
    assert r.fields["invoice_number"].value is None and r.issues[0].rule_code == "EXTRACT_NO_SNIPPET"


def test_an_ambiguous_date_is_not_guessed():
    text = "Date: 03/04/2026\n"  # 3 April or 4 March? the text does not say
    for guess in ("2026-03-04", "2026-04-03"):
        r = extract(MockLLM([response(invoice_date=field(guess, "Date: 03/04/2026"))]), text)
        assert r.fields["invoice_date"].value is None
    clear = extract(MockLLM([response(invoice_date=field("2026-04-23", "Date: 23/04/2026"))]), "Date: 23/04/2026\n")
    assert str(clear.fields["invoice_date"].value) == "2026-04-23"  # 23 cannot be a month: unambiguous


def test_known_limit_a_real_value_under_the_wrong_label_cannot_be_caught_by_code():
    """Documents what the checks do NOT cover: the snippet and the value are both genuinely in the text, so
    nothing deterministic can tell that 'ORD-555' is an order number, not an invoice number. That judgement
    belongs to the prompt and the model, and to validation rules downstream (duplicates, cross-document)."""
    text = "Order number: ORD-555\nDate: 2026-02-03\n"
    r = extract(MockLLM([response(invoice_number=field("ORD-555", "Order number: ORD-555"))]), text)
    assert r.fields["invoice_number"].value == "ORD-555"


def test_the_demo_extractor_also_returns_nulls_for_text_without_invoice_data():
    d = extract_demo(NOT_AN_INVOICE)
    assert all(d[n]["value"] is None for n in SCALARS) and d["line_items"] == []
    d = extract_demo("Subtotal of items\nTotal due\n")
    assert d["total"]["value"] is None and d["subtotal"]["value"] is None  # labels without amounts give nothing


# ---- end to end: save, validate, status ------------------------------------------------------------------------------

@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with Session(sa.create_engine(url)) as s:
        yield s
        s.rollback()
        s.execute(sa.delete(Document).where(Document.original_filename.like("nofab-%")))
        s.commit()


def doc_with_text(session, text) -> Document:
    d = Document(original_filename="nofab-a.pdf", stored_path="/x", file_type="pdf", file_size=1,
                 file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), status=DocumentStatus.processing)
    session.add(d)
    session.flush()
    session.add(DocumentPage(document_id=d.id, page_number=1, extracted_text=text, extraction_method=ExtractionMethod.digital))
    session.commit()
    return d


def test_end_to_end_a_hallucinating_model_leaves_an_empty_result_and_a_document_for_review(session, monkeypatch):
    monkeypatch.setattr(settings, "llm_backoff_base_seconds", 0.0)
    d = doc_with_text(session, NOT_AN_INVOICE)
    assert es.extract_document(session, d.id, MockLLM([hallucination()])).ok
    r = session.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == d.id)).one()
    assert [getattr(r, n) for n in SCALARS] == [None] * 8 and r.line_items == []
    assert r.field_confidence["total"] == {"confidence": 0.0, "source_snippet": None}
    assert "Total: 110.00 USD" in str(r.raw_llm_output)  # the raw lie is kept for audit, but never used
    out = validation_service.validate_document(session, d.id)
    session.refresh(d)
    assert d.status == DocumentStatus.needs_review and out.errors == 4  # nothing found: a human must look
    missing = {f.field for f in session.scalars(sa.select(VErr).where(VErr.document_id == d.id, VErr.rule_code == "MISSING_FIELD",
                                                                      VErr.severity == "error"))}
    assert missing == {"invoice_number", "invoice_date", "supplier_name", "total"}


def test_end_to_end_a_partly_readable_document_keeps_only_what_is_true(session):
    d = doc_with_text(session, "INVOICE\nInvoice No: INV-7\nTotal: 55.00 EUR\n")
    llm = MockLLM([hallucination() | {"invoice_number": field("INV-7", "Invoice No: INV-7"),
                                      "total": field("55.00", "Total: 55.00 EUR")}])
    es.extract_document(session, d.id, llm)
    r = session.scalars(sa.select(ExtractedResult).where(ExtractedResult.document_id == d.id)).one()
    assert (r.invoice_number, str(r.total), r.supplier_name, r.invoice_date, r.subtotal, r.tax) == ("INV-7", "55.00", None, None, None, None)
