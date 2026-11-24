"""Each validation rule on its own, with good and bad data (no DB)."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.core.config import settings
from app.models.enums import Severity
from app.services import validation_service as vs
from app.services.validation_service import InvoiceView, ItemView

E, W = Severity.error, Severity.warning


def good(**kw) -> InvoiceView:
    base = dict(invoice_number="INV-1", invoice_date=date(2026, 1, 5), supplier_name="Acme", customer_name="Rahim",
                subtotal=D("100.00"), tax=D("5.00"), total=D("105.00"), currency="USD",
                items=[ItemView(1, "A", D("2"), D("30.00"), D("60.00")), ItemView(2, "B", D("1"), D("40.00"), D("40.00"))])
    base.update(kw)
    return InvoiceView(**base)


def codes(findings):
    return [(f.rule_code, f.severity, f.field) for f in findings]


def test_a_fully_good_invoice_passes_every_pure_rule():
    for rule in vs.PURE_RULES:
        assert (rule(good()) if rule is not vs.rule_invalid_date else rule(good(), date(2026, 6, 1))) == []


# 1. MISSING_FIELD
@pytest.mark.parametrize("name", ["invoice_number", "invoice_date", "supplier_name", "total"])
def test_missing_required_field_is_error(name):
    assert codes(vs.rule_missing_field(good(**{name: None}))) == [("MISSING_FIELD", E, name)]


@pytest.mark.parametrize("name", ["customer_name", "currency"])
def test_missing_optional_field_is_warning(name):
    assert codes(vs.rule_missing_field(good(**{name: None}))) == [("MISSING_FIELD", W, name)]


def test_blank_string_counts_as_missing():
    assert codes(vs.rule_missing_field(good(supplier_name="   "))) == [("MISSING_FIELD", E, "supplier_name")]


def test_everything_missing():
    assert len(vs.rule_missing_field(InvoiceView())) == 6


# 2. INVALID_NUMBER
def test_negative_amount_is_error():
    assert codes(vs.rule_invalid_number(good(tax=D("-1.00")))) == [("INVALID_NUMBER", E, "tax")]


def test_unusually_large_amount_is_error(monkeypatch):
    assert codes(vs.rule_invalid_number(good(total=settings.validation_max_amount + 1)))[0][:2] == ("INVALID_NUMBER", E)
    assert vs.rule_invalid_number(good(total=settings.validation_max_amount)) == []


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), 12.5])
def test_non_number_is_error(bad):
    assert codes(vs.rule_invalid_number(good(subtotal=bad))) == [("INVALID_NUMBER", E, "subtotal")]


def test_invalid_numbers_in_line_items_are_found():
    items = [ItemView(1, "A", D("-1"), D("10"), D("10")), ItemView(2, "B", D("1"), D("10"), D("NaN"))]
    assert [f.field for f in vs.rule_invalid_number(good(items=items))] == [
        "line_items[1].quantity", "line_items[2].line_total"]


def test_zero_and_null_are_fine():
    assert vs.rule_invalid_number(good(tax=D("0"), subtotal=None)) == []


# 3. LINE_ITEM_MISMATCH
def test_line_item_matches_within_tolerance():
    ok = [ItemView(1, "A", D("3"), D("0.335"), D("1.01"))]  # 1.005 vs 1.01: off by 0.005
    assert vs.rule_line_item_mismatch(good(items=ok)) == []
    edge = [ItemView(1, "A", D("1"), D("10.00"), D("10.02"))]  # exactly 0.02 is allowed
    assert vs.rule_line_item_mismatch(good(items=edge)) == []


def test_line_item_mismatch_beyond_tolerance():
    bad = [ItemView(1, "A", D("2"), D("30.00"), D("60.03")), ItemView(2, "B", D("1"), D("40"), D("40"))]
    f = vs.rule_line_item_mismatch(good(items=bad))
    assert codes(f) == [("LINE_ITEM_MISMATCH", E, "line_items[1].line_total")]
    assert "60.00" in f[0].message and "60.03" in f[0].message


def test_line_item_with_missing_numbers_is_skipped():
    assert vs.rule_line_item_mismatch(good(items=[ItemView(1, "A", None, D("30"), D("60"))])) == []


def test_line_item_uses_decimal_not_float():
    # 0.1 * 3 is 0.30000000000000004 in float; with Decimal it is exactly 0.3.
    assert vs.rule_line_item_mismatch(good(items=[ItemView(1, "A", D("3"), D("0.1"), D("0.30"))])) == []


# 4. SUBTOTAL_MISMATCH
def test_subtotal_matches_sum_of_lines():
    assert vs.rule_subtotal_mismatch(good()) == []
    assert vs.rule_subtotal_mismatch(good(subtotal=D("100.02"))) == []


def test_subtotal_mismatch():
    f = vs.rule_subtotal_mismatch(good(subtotal=D("120.00")))
    assert codes(f) == [("SUBTOTAL_MISMATCH", E, "subtotal")] and "100.00" in f[0].message


def test_subtotal_not_checked_when_it_cannot_be_checked():
    partial = [ItemView(1, "A", D("1"), D("5"), None)]
    assert vs.rule_subtotal_mismatch(good(items=partial)) == []
    assert vs.rule_subtotal_mismatch(good(items=[])) == []
    assert vs.rule_subtotal_mismatch(good(subtotal=None)) == []


# 5. TOTAL_MISMATCH
def test_total_is_subtotal_plus_tax():
    assert vs.rule_total_mismatch(good()) == []
    assert vs.rule_total_mismatch(good(total=D("105.02"))) == []


def test_total_mismatch():
    f = vs.rule_total_mismatch(good(total=D("110.00")))
    assert codes(f) == [("TOTAL_MISMATCH", E, "total")] and "105.00" in f[0].message


@pytest.mark.parametrize("missing", ["subtotal", "tax", "total"])
def test_total_not_checked_if_a_part_is_unknown(missing):
    assert vs.rule_total_mismatch(good(**{missing: None})) == []


# 7. LOW_CONFIDENCE
def test_low_confidence_warns():
    inv = good(confidence={"invoice_number": 0.65, "total": 0.7, "supplier_name": 0.99}, item_confidence={2: 0.4})
    assert codes(vs.rule_low_confidence(inv)) == [
        ("LOW_CONFIDENCE", W, "invoice_number"), ("LOW_CONFIDENCE", W, "line_items[2]")]


def test_confidence_of_null_values_is_ignored():
    assert vs.rule_low_confidence(good(customer_name=None, confidence={"customer_name": 0.0})) == []


# 8. INVALID_DATE
TODAY = date(2026, 6, 1)


def test_valid_past_and_today_dates_pass():
    assert vs.rule_invalid_date(good(invoice_date=TODAY), TODAY) == []
    assert vs.rule_invalid_date(good(invoice_date=date(2020, 1, 1)), TODAY) == []


def test_future_date_warns():
    assert codes(vs.rule_invalid_date(good(invoice_date=date(2026, 6, 2)), TODAY)) == [("INVALID_DATE", W, "invoice_date")]


def test_unparseable_date_text_warns():
    f = vs.rule_invalid_date(good(invoice_date=None, raw_date_text="31/31/2026"), TODAY)
    assert codes(f) == [("INVALID_DATE", W, "invoice_date")] and "31/31/2026" in f[0].message


def test_no_date_at_all_is_not_this_rules_business():
    assert vs.rule_invalid_date(good(invoice_date=None), TODAY) == []


def test_view_reads_raw_date_text_from_llm_output():
    class R:  # minimal stand-in for an ExtractedResult
        id = document_id = None
        invoice_index = 0
        invoice_number = invoice_date = supplier_name = customer_name = currency = None
        subtotal = tax = total = None
        line_items = []
        field_confidence = {"invoice_number": {"confidence": 0.5}, "line_items": [{"position": 1, "confidence": 0.2}]}
        raw_llm_output = {"parsed": {"invoice_date": {"value": "soon"}}}

    v = InvoiceView.from_result(R())
    assert v.raw_date_text == "soon" and v.confidence == {"invoice_number": 0.5} and v.item_confidence == {1: 0.2}
