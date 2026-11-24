from datetime import date
from decimal import Decimal

import pytest

from app.services.field_verification import (
    normalize, numbers_in, parse_date_strict, parse_decimal_lenient, snippet_in_text, verify_decimal, verify_field,
)

TEXT = normalize("""ACME Ltd
Invoice No: INV-2026-0042
Date: 24 Nov 2026
Total:  1,250.50 EUR
""")


@pytest.mark.parametrize("raw,expected", [
    ("1,250.50", "1250.50"), ("1.250,50", "1250.50"), ("1250,50", "1250.50"), ("$ 12", "12"),
    ("USD 12.5", "12.5"), ("1,234,567", "1234567"), ("-5.00", "-5.00"), ("(5.00)", "-5.00"),
    ("1'250.50", "1250.50"),
])
def test_parse_decimal(raw, expected):
    assert parse_decimal_lenient(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1,250", "1.250", "abc", "", "12,34,5"])
def test_parse_decimal_ambiguous_is_none(raw):
    assert parse_decimal_lenient(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-24", date(2026, 11, 24)), ("2026/11/24", date(2026, 11, 24)), ("24.11.2026", date(2026, 11, 24)),
    ("24/11/2026", date(2026, 11, 24)), ("11/24/2026", date(2026, 11, 24)), ("24 Nov 2026", date(2026, 11, 24)),
    ("November 24, 2026", date(2026, 11, 24)),
])
def test_parse_date(raw, expected):
    assert parse_date_strict(raw) == expected


@pytest.mark.parametrize("raw", ["03/04/2026", "2026-13-01", "31.02.2026", "soon", ""])
def test_ambiguous_or_invalid_dates_are_none(raw):
    assert parse_date_strict(raw) is None


def test_numbers_in_snippet():
    assert Decimal("1250.50") in numbers_in("Total:  1,250.50 EUR")


def test_snippet_matching_ignores_case_and_whitespace():
    assert snippet_in_text("invoice no:\n  INV-2026-0042", TEXT)
    assert not snippet_in_text("Invoice No: INV-9999", TEXT)
    assert not snippet_in_text("", TEXT) and not snippet_in_text(None, TEXT)


def test_null_value_stays_null():
    v = verify_field("total", None, 0.9, "x", TEXT)
    assert v.value is None and v.confidence == 0.0 and v.rejected_reason is None


def test_value_without_snippet_is_rejected():
    v = verify_field("invoice_number", "INV-2026-0042", 0.9, None, TEXT)
    assert v.value is None and v.rejected_reason == "EXTRACT_NO_SNIPPET"


def test_fabricated_snippet_is_rejected():
    v = verify_field("invoice_number", "INV-7", 0.9, "Invoice No: INV-7", TEXT)
    assert v.value is None and v.rejected_reason == "EXTRACT_SNIPPET_NOT_FOUND"


def test_value_must_be_inside_its_snippet():
    v = verify_field("invoice_number", "INV-OTHER", 0.9, "Invoice No: INV-2026-0042", TEXT)
    assert v.value is None and v.rejected_reason == "EXTRACT_VALUE_NOT_IN_SNIPPET"


def test_good_values_are_typed():
    assert verify_field("invoice_number", "INV-2026-0042", 0.9, "Invoice No: INV-2026-0042", TEXT).value == "INV-2026-0042"
    assert verify_field("total", "1250.50", 0.8, "Total:  1,250.50 EUR", TEXT).value == Decimal("1250.50")
    assert verify_field("invoice_date", "2026-11-24", 0.9, "Date: 24 Nov 2026", TEXT).value == date(2026, 11, 24)
    assert verify_field("currency", "eur", 0.9, "Total:  1,250.50 EUR", TEXT).value == "EUR"


def test_wrong_amount_for_snippet_is_rejected():
    v = verify_field("total", "1350.50", 0.9, "Total:  1,250.50 EUR", TEXT)
    assert v.value is None and v.rejected_reason == "EXTRACT_VALUE_NOT_IN_SNIPPET"


def test_date_must_be_iso_and_valid():
    assert verify_field("invoice_date", "24/11/2026", 0.9, "Date: 24 Nov 2026", TEXT).rejected_reason == "EXTRACT_INVALID_DATE"
    assert verify_field("invoice_date", "2026-02-30", 0.9, "Date: 24 Nov 2026", TEXT).rejected_reason == "EXTRACT_INVALID_DATE"


def test_bad_currency_code():
    assert verify_field("currency", "euro", 0.9, "Total:  1,250.50 EUR", TEXT).rejected_reason == "EXTRACT_INVALID_CURRENCY"


def test_verify_decimal():
    assert verify_decimal("31.00", "Domain 2 x 15.50 = 31.00", TEXT) == (Decimal("31.00"), None)
    assert verify_decimal("99", "Domain 2 x 15.50 = 31.00", TEXT) == (None, "EXTRACT_VALUE_NOT_IN_SNIPPET")
    assert verify_decimal(None, "x", TEXT) == (None, None)
