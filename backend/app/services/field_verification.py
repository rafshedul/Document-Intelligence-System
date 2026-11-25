"""Deterministic checks on LLM (or spreadsheet-mapped) values.

The rule: a value survives only if its source_snippet really occurs in the
document text and, where we can check, the value really occurs in the snippet.
Anything else becomes null, with a reason.
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.extraction_schema import MONEY_FIELDS

_WS = re.compile(r"\s+")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_NUM_TOKEN = re.compile(r"-?\d[\d., ' ]*\d|-?\d")


def normalize(text: str) -> str:
    """Case-fold, unify unicode forms and collapse all whitespace (OCR line breaks)."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def snippet_in_text(snippet: str | None, normalized_text: str) -> bool:
    if not snippet or not snippet.strip():
        return False
    return normalize(snippet) in normalized_text


# ---- parsing -----------------------------------------------------------------

def parse_decimal_lenient(raw: str) -> Decimal | None:
    """Parse '1,250.50', '1.250,50', '1250,50', '$ 12', 'USD 12.5'. None if ambiguous.

    A lone separator followed by exactly three digits ('1,250') is ambiguous
    (thousands or decimal?) and returns None rather than guessing.
    """
    s = re.sub(r"[^\d.,\-]", "", raw.replace(" ", "").replace(" ", "").replace("'", ""))
    neg = s.startswith("-") or (raw.strip().startswith("(") and raw.strip().endswith(")"))
    s = s.replace("-", "")
    if not s or not any(c.isdigit() for c in s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2:  # 1,234,567: thousands
            if not all(len(p) == 3 for p in parts[1:]):
                return None
            s = "".join(parts)
        elif len(parts[1]) == 3:
            return None  # ambiguous
        else:
            s = parts[0] + "." + parts[1]
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def numbers_in(text: str) -> list[Decimal]:
    out = []
    for tok in _NUM_TOKEN.findall(text):
        d = parse_decimal_lenient(tok)
        if d is not None:
            out.append(d)
    return out


_DATE_NAMED = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%B-%Y")


def parse_date_strict(raw: str) -> date | None:
    """YYYY-MM-DD and unambiguous variants; None if day/month order can't be known."""
    raw = raw.strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)  # dotted = day first
    if m:
        return _mk(int(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", raw)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 >= b:
            return _mk(y, b, a)
        if b > 12 >= a:
            return _mk(y, a, b)
        return None  # both <= 12 (or both invalid): ambiguous
    for fmt in _DATE_NAMED:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


_MONTHS = {m: i for i, names in enumerate(
    ["jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august",
     "sep sept september", "oct october", "nov november", "dec december"], start=1) for m in names.split()}
_NUMERIC_DATE = re.compile(r"(?<![\d.])(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?![\d])")
_DAY_MONTH_YEAR = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MONTH_DAY_YEAR = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


def dates_in(text: str) -> list[date | None]:
    """Every date written in `text`, as a date, or None where its day/month order or year cannot be known
    (03/04/2026, 03/04/26). Formats it does not recognise are simply not listed."""
    found: list[date | None] = [parse_date_strict(t) for t in _NUMERIC_DATE.findall(text)]
    for d, mon, y in _DAY_MONTH_YEAR.findall(text):
        if mon.lower() in _MONTHS:
            found.append(_mk(int(y), _MONTHS[mon.lower()], int(d)))
    for mon, d, y in _MONTH_DAY_YEAR.findall(text):
        if mon.lower() in _MONTHS:
            found.append(_mk(int(y), _MONTHS[mon.lower()], int(d)))
    return found


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


# ---- verification --------------------------------------------------------------

@dataclass
class Verified:
    value: Any | None  # str | date | Decimal | None
    confidence: float
    source_snippet: str | None
    rejected_reason: str | None = None  # why a non-null value was dropped


def _reject(reason: str) -> Verified:
    return Verified(None, 0.0, None, reason)


def verify_field(name: str, value: str | None, confidence: float, snippet: str | None, norm_text: str) -> Verified:
    """Apply rules 1-2: no value without a snippet that exists in the text."""
    if value is None or not str(value).strip():
        return Verified(None, 0.0, None)
    value = str(value).strip()
    if not snippet or not snippet.strip():
        return _reject("EXTRACT_NO_SNIPPET")
    if not snippet_in_text(snippet, norm_text):
        return _reject("EXTRACT_SNIPPET_NOT_FOUND")

    typed: Any
    if name == "invoice_date":
        typed = parse_date_strict(value)
        if typed is None or typed.isoformat() != value:
            return _reject("EXTRACT_INVALID_DATE")
        written = dates_in(snippet)
        if written and typed not in written:  # the text has a date, and it is not (or not provably) this one
            return _reject("EXTRACT_DATE_NOT_IN_SNIPPET")
    elif name in MONEY_FIELDS:
        try:
            typed = Decimal(value)
        except InvalidOperation:
            return _reject("EXTRACT_INVALID_NUMBER")
        if typed not in numbers_in(snippet):
            return _reject("EXTRACT_VALUE_NOT_IN_SNIPPET")
    elif name == "currency":
        typed = value.upper()
        if not _CURRENCY.match(typed):
            return _reject("EXTRACT_INVALID_CURRENCY")
    else:  # invoice_number, supplier_name, customer_name
        typed = value
        if normalize(value) not in normalize(snippet):
            return _reject("EXTRACT_VALUE_NOT_IN_SNIPPET")
    return Verified(typed, confidence, snippet.strip())


def verify_decimal(raw: str | None, snippet: str | None, norm_text: str) -> tuple[Decimal | None, str | None]:
    """For line-item numbers: returns (value, reject_reason)."""
    if raw is None:
        return None, None
    try:
        d = Decimal(raw)
    except InvalidOperation:
        return None, "EXTRACT_INVALID_NUMBER"
    if d not in numbers_in(snippet or ""):
        return None, "EXTRACT_VALUE_NOT_IN_SNIPPET"
    return d, None
