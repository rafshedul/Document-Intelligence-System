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


# ---- dates must match what the snippet says -----------------------------------------------------------------------

from app.services.field_verification import dates_in  # noqa: E402


def test_dates_in_text():
    assert dates_in("Date: 2026-11-24") == [date(2026, 11, 24)]
    assert dates_in("24 Nov 2026, due 2nd December 2026") == [date(2026, 11, 24), date(2026, 12, 2)]
    assert dates_in("November 24, 2026") == [date(2026, 11, 24)] and dates_in("Nov. 24 2026") == [date(2026, 11, 24)]
    assert dates_in("24.11.2026") == [date(2026, 11, 24)] and dates_in("24/11/2026") == [date(2026, 11, 24)]
    assert dates_in("03/04/2026") == [None] and dates_in("03/04/26") == [None]  # order or year unknowable
    assert dates_in("no date here, only 12 items and 3.50") == []
    assert dates_in("31 Feb 2026") == [None]  # not a real day


@pytest.mark.parametrize("snippet,value,ok", [
    ("Date: 2026-11-24", "2026-11-24", True),
    ("Date: 24 Nov 2026", "2026-11-24", True),
    ("Date: November 24, 2026", "2026-11-24", True),
    ("Date: 24/11/2026", "2026-11-24", True),
    ("Date: 11/24/2026", "2026-11-24", True),       # 24 cannot be a month
    ("Date: 03/04/2026", "2026-03-04", False),      # guess: ambiguous
    ("Date: 03/04/2026", "2026-04-03", False),      # the other guess: also rejected
    ("Date: 03/04/26", "2026-04-03", False),
    ("Date: 24 Nov 2026", "2026-11-25", False),     # wrong day
    ("Date: 2026-11-24", "2026-11-04", False),
    ("Issued 5th of May 2026", "2026-05-05", True),
    ("Datum: 5. Mai", "2026-05-05", True),          # no recognisable full date to contradict: not second-guessed
])
def test_a_date_value_must_agree_with_the_dates_written_in_its_snippet(snippet, value, ok):
    text = normalize(snippet + "\n")
    v = verify_field("invoice_date", value, 0.9, snippet, text)
    assert (v.value is not None) == ok
    if not ok:
        assert v.rejected_reason == "EXTRACT_DATE_NOT_IN_SNIPPET"
