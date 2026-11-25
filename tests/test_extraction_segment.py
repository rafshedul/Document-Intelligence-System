"""extract_segment: the LLM contract, with a mock LLM (no DB)."""
import json
from datetime import date
from decimal import Decimal

import pytest
from mocks import MockLLM, field, response

from app.core.config import settings
from app.models.enums import Severity
from app.services.extraction_service import extract_segment
from app.services.llm_client import CallStats, LLMError, LLMRateLimitError, LLMRefusalError, RateGate

TEXT = """INVOICE
Invoice No: INV-2026-0042
Date: 2026-11-24
Supplier: Acme Hosting Ltd
Customer: Rahim Traders
Web hosting      1 x 240.00 = 240.00
Domain renewal   2 x 15.50 = 31.00
Subtotal: 271.00
Tax (5%): 13.55
TOTAL: 284.55 USD
"""

GOOD = response(
    invoice_number=field("INV-2026-0042", "Invoice No: INV-2026-0042", 0.99),
    invoice_date=field("2026-11-24", "Date: 2026-11-24"),
    supplier_name=field("Acme Hosting Ltd", "Supplier: Acme Hosting Ltd"),
    customer_name=field("Rahim Traders", "Customer: Rahim Traders"),
    subtotal=field("271.00", "Subtotal: 271.00"),
    tax=field("13.55", "Tax (5%): 13.55"),
    total=field("284.55", "TOTAL: 284.55 USD"),
    currency=field("USD", "TOTAL: 284.55 USD"),
    line_items=[
        {"description": "Web hosting", "quantity": "1", "unit_price": "240.00", "line_total": "240.00",
         "confidence": 0.9, "source_snippet": "Web hosting      1 x 240.00 = 240.00"},
        {"description": "Domain renewal", "quantity": "2", "unit_price": "15.50", "line_total": "31.00",
         "confidence": 0.85, "source_snippet": "Domain renewal   2 x 15.50 = 31.00"},
    ],
)


def run(llm, text=TEXT):
    return extract_segment(llm, text, 0, 1, 1, RateGate(), CallStats())


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "llm_backoff_base_seconds", 0.0)


def test_good_extraction_is_typed_and_complete():
    llm = MockLLM([GOOD])
    r = run(llm)
    f = r.fields
    assert f["invoice_number"].value == "INV-2026-0042" and f["invoice_number"].confidence == 0.99
    assert f["invoice_date"].value == date(2026, 11, 24)
    assert f["total"].value == Decimal("284.55") and f["currency"].value == "USD"
    assert f["total"].source_snippet == "TOTAL: 284.55 USD"
    assert [(i.position, i.description, i.line_total) for i in r.items] == [
        (1, "Web hosting", Decimal("240.00")), (2, "Domain renewal", Decimal("31.00"))]
    assert not r.needs_review and r.issues == [] and len(llm.calls) == 1
    assert json.loads(r.raw["attempts"][0]["output"])["invoice_number"]["value"] == "INV-2026-0042"


def test_call_asks_for_deterministic_json_and_wraps_the_text():
    llm = MockLLM([GOOD])
    run(llm)
    c = llm.calls[0]
    assert c["temperature"] == 0.0
    assert "<document>" in c["messages"][0]["content"] and "INV-2026-0042" in c["messages"][0]["content"]
    assert "null" in c["system"] and "source_snippet" in c["system"]
    assert "properties" in c["schema"] or "$defs" in c["schema"]


def test_fabricated_values_become_null():
    bad = response(
        invoice_number=field("INV-9999", "Invoice No: INV-9999"),            # snippet not in text
        supplier_name=field("Globex", None),                                 # no snippet
        total=field("999.99", "TOTAL: 284.55 USD"),                          # value not in snippet
        customer_name=field("Rahim Traders", "Customer: Rahim Traders"),     # fine
    )
    r = run(MockLLM([bad]))
    assert r.fields["invoice_number"].value is None
    assert r.fields["supplier_name"].value is None
    assert r.fields["total"].value is None
    assert r.fields["customer_name"].value == "Rahim Traders"
    codes = {i.rule_code for i in r.issues}
    assert codes == {"EXTRACT_SNIPPET_NOT_FOUND", "EXTRACT_NO_SNIPPET", "EXTRACT_VALUE_NOT_IN_SNIPPET"}
    assert all(i.severity == Severity.warning for i in r.issues)
    assert not r.needs_review  # discarding values is not a failure


def test_line_item_with_fake_snippet_is_dropped_and_numbers_checked():
    items = [
        {"description": "Ghost", "quantity": "1", "unit_price": "5", "line_total": "5", "confidence": 0.9,
         "source_snippet": "Ghost service 1 x 5.00"},
        {"description": "Web hosting", "quantity": "1", "unit_price": "240.00", "line_total": "999.00",
         "confidence": 0.9, "source_snippet": "Web hosting      1 x 240.00 = 240.00"},
    ]
    r = run(MockLLM([response(line_items=items)]))
    assert len(r.items) == 1 and r.items[0].description == "Web hosting"
    assert r.items[0].line_total is None and r.items[0].unit_price == Decimal("240.00")
    assert {i.rule_code for i in r.issues} == {"EXTRACT_LINE_ITEM_DROPPED", "EXTRACT_VALUE_NOT_IN_SNIPPET"}


def test_invalid_json_is_retried_with_the_error_and_then_succeeds():
    llm = MockLLM(["not json at all", GOOD])
    r = run(llm)
    assert len(llm.calls) == 2 and not r.needs_review
    followup = llm.calls[1]["messages"]
    assert [m["role"] for m in followup] == ["user", "assistant", "user"]
    assert "invalid" in followup[2]["content"].lower() and followup[1]["content"] == "not json at all"
    assert r.raw["attempts"][0]["error"] and r.raw["attempts"][1]["error"] is None


def test_schema_violations_are_retried():
    bad = response(total=field("1,250.50", "TOTAL: 284.55"))  # thousands separator: not a plain decimal
    llm = MockLLM([bad, GOOD])
    r = run(llm)
    assert len(llm.calls) == 2 and r.fields["total"].value == Decimal("284.55")
    assert "total" in r.raw["attempts"][0]["error"]


def test_gives_up_after_two_retries_and_flags_review():
    llm = MockLLM(["{broken"])
    r = run(llm)
    assert len(llm.calls) == 3  # first try + 2 retries
    assert r.needs_review and r.fields == {}
    assert r.issues[0].rule_code == "EXTRACT_LLM_OUTPUT_INVALID" and r.issues[0].severity == Severity.error
    assert len(r.raw["attempts"]) == 3


def test_missing_and_extra_keys_are_invalid():
    broken = response()
    del broken["currency"]
    assert run(MockLLM([broken])).needs_review
    extra = response(); extra["surprise"] = 1
    assert run(MockLLM([extra])).needs_review


def test_rate_limits_are_retried_without_using_validation_retries():
    llm = MockLLM([LLMRateLimitError("slow", retry_after=0.0), GOOD])
    r = run(llm)
    assert len(llm.calls) == 2 and not r.needs_review


def test_refusal_needs_review():
    r = run(MockLLM([LLMRefusalError("declined")]))
    assert r.needs_review and r.issues[0].rule_code == "EXTRACT_LLM_REFUSED"


def test_fatal_provider_error_is_reported():
    r = run(MockLLM([LLMError("invalid api key")]))
    assert r.fatal == "invalid api key" and not r.needs_review


def test_oversized_segment_is_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "extraction_max_segment_chars", 50)
    llm = MockLLM([GOOD])
    r = run(llm)
    assert r.needs_review and llm.calls == [] and r.issues[0].rule_code == "EXTRACT_SEGMENT_TOO_LARGE"


def test_multiple_invoices_warning():
    r = run(MockLLM([response(multiple_invoices_detected=True)]))
    assert any(i.rule_code == "EXTRACT_MULTIPLE_INVOICES" for i in r.issues)


def test_ocr_whitespace_differences_do_not_reject_a_correct_snippet():
    llm = MockLLM([response(invoice_number=field("INV-2026-0042", "invoice no:   INV-2026-0042"))])
    assert run(llm).fields["invoice_number"].value == "INV-2026-0042"


def test_a_line_item_description_must_be_inside_its_snippet():
    item = {"description": "Gold plated hosting", "quantity": "1", "unit_price": "240.00", "line_total": "240.00",
            "confidence": 0.9, "source_snippet": "Web hosting      1 x 240.00 = 240.00"}
    r = run(MockLLM([response(line_items=[item])]))
    assert len(r.items) == 1 and r.items[0].description is None and r.items[0].line_total == Decimal("240.00")
    assert "EXTRACT_VALUE_NOT_IN_SNIPPET" in {i.rule_code for i in r.issues}
