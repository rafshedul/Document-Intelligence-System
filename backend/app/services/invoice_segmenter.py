"""Split a document's pages into one segment per invoice (no LLM needed).

Looks at each page for an invoice number and an invoice-style header:
- a page whose invoice number differs from the current invoice's starts a new one;
- a page with an invoice header and no number starts a new one when the
  current invoice already looks complete (has a total) and the page isn't
  marked "continued" / "page 2 of 3";
- everything else continues the current invoice.
"""
import re
from dataclasses import dataclass, field

_NUMBER = re.compile(
    r"\b(?:tax\s+)?(?:invoice|inv|bill|receipt)\s*(?:no\.?|number|num\.?|#|nr\.?|id)?\s*[:#.\-]?\s*"
    r"([A-Z0-9][A-Z0-9\-/_.]{2,24})\b",
    re.IGNORECASE,
)
_HEADER = re.compile(r"^\W*(?:tax\s+)?(?:invoice|bill|receipt|credit\s+note)\b", re.IGNORECASE)
_CONTINUED = re.compile(r"\bcontinued\b|\bcont\.|\bpage\s*(?:[2-9]|\d{2,})\s*(?:of|/)\s*\d+", re.IGNORECASE)
_HAS_TOTAL = re.compile(r"\b(?:grand\s+)?total\b|\bamount\s+due\b|\bbalance\s+due\b", re.IGNORECASE)
_STOP_WORDS = {"date", "number", "total", "from", "to", "for", "details", "summary", "amount", "no", "and", "the",
               "issued", "due", "terms", "payment", "date:", "info", "information"}


@dataclass
class Segment:
    page_start: int
    page_end: int
    text: str
    invoice_hint: str | None = None
    pages: list[int] = field(default_factory=list)


def find_invoice_number(text: str) -> str | None:
    for m in _NUMBER.finditer(text):
        cand = m.group(1).strip(".-/_")
        if cand.lower() in _STOP_WORDS or not any(c.isdigit() for c in cand):
            continue  # "Invoice date" etc.; real numbers contain a digit
        return cand.upper()
    return None


def has_header(text: str, lines: int = 12) -> bool:
    head = [ln for ln in text.splitlines() if ln.strip()][:lines]
    return any(_HEADER.match(ln) for ln in head)


def split_invoices(pages: list[tuple[int, str]]) -> list[Segment]:
    """pages: [(page_number, text)] in order. Blank pages are skipped."""
    segments: list[Segment] = []
    cur: Segment | None = None
    cur_complete = False
    for number, text in pages:
        if not text.strip():
            continue
        inv = find_invoice_number(text)
        header = has_header(text)
        continued = bool(_CONTINUED.search(text[:600]))
        start = False
        if cur is None:
            start = True
        elif inv and cur.invoice_hint and inv != cur.invoice_hint:
            start = True
        elif not inv and header and cur_complete and not continued:
            start = True
        if start:
            cur = Segment(number, number, text, inv, [number])
            segments.append(cur)
            cur_complete = False
        else:
            cur.page_end = number
            cur.text += "\n\n" + text
            cur.pages.append(number)
            if inv and not cur.invoice_hint:
                cur.invoice_hint = inv
        cur_complete = bool(_HAS_TOTAL.search(text))
    return segments
