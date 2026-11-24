"""Map spreadsheet columns to invoice fields by header name, without an LLM.

Works on the text that spreadsheet_processor stored in document_pages
(`# Sheet: name (rows a-b)` followed by tab-separated rows). A sheet is mappable
when it has an invoice-number column and a total column. Otherwise the caller
sends that sheet to the LLM.

A sheet that also has item columns (description / quantity / unit price) is
treated as a line-item table: rows are grouped by invoice number, and the total
column is only used as an *invoice* total if it is named like one ("invoice
total", "grand total"), because a bare "Total" there is a line total.
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.services.field_verification import parse_date_strict, parse_decimal_lenient

_SHEET_HEADER = re.compile(r"^# Sheet: (.*) \(rows (\d+)-(\d+)\)$")


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", header.casefold()).strip()


SYNONYMS: dict[str, set[str]] = {
    "invoice_number": {"invoice no", "invoice number", "invoice nr", "invoice num", "invoice", "invoice id", "inv no",
                       "inv number", "inv", "bill no", "bill number", "document no", "document number",
                       "rechnung", "rechnungsnummer", "rechnungsnr", "rechnung nr"},
    "invoice_date": {"invoice date", "date", "inv date", "bill date", "issue date", "date of issue", "datum",
                     "rechnungsdatum"},
    "supplier_name": {"supplier", "supplier name", "vendor", "vendor name", "seller", "from", "lieferant"},
    "customer_name": {"customer", "customer name", "client", "client name", "buyer", "bill to", "kunde"},
    "subtotal": {"subtotal", "sub total", "net", "net amount", "zwischensumme"},
    "tax": {"tax", "tax amount", "vat", "vat amount", "gst", "sales tax", "steuer", "mwst"},
    "total": {"total", "invoice total", "grand total", "total amount", "amount", "betrag", "gesamt", "gesamtbetrag",
              "total due", "amount due"},
    "currency": {"currency", "curr", "currency code", "währung"},
    "description": {"description", "item", "item description", "product", "service", "details", "beschreibung",
                    "artikel"},
    "quantity": {"quantity", "qty", "units", "menge"},
    "unit_price": {"unit price", "price", "rate", "unit cost", "unit rate", "einzelpreis"},
    "line_total": {"line total", "line amount", "amount", "extended price", "ext price"},
}
INVOICE_TOTAL_KEYS = {"invoice total", "grand total", "total due", "amount due", "gesamtbetrag"}
LINE_FIELDS = ("description", "quantity", "unit_price")


@dataclass
class MappedField:
    value: object
    snippet: str  # the raw cell text (it occurs in the page text)


@dataclass
class MappedInvoice:
    sheet: str
    row_start: int
    row_end: int
    fields: dict[str, MappedField] = field(default_factory=dict)
    line_items: list[dict[str, MappedField]] = field(default_factory=list)


@dataclass
class Sheet:
    name: str
    first_row: int
    rows: list[list[str]]  # row i has number first_row + i... but blank rows were dropped; see row_numbers
    row_numbers: list[int]
    pages: list[int]


@dataclass
class SheetPlan:
    sheet: Sheet
    invoices: list[MappedInvoice] | None  # None: not mappable, use the LLM
    reason: str = ""


def rebuild_sheets(pages: list[tuple[int, str]]) -> list[Sheet]:
    """Reassemble sheets from document_pages text (a sheet may span several pages)."""
    sheets: dict[str, Sheet] = {}
    order: list[str] = []
    for page_no, text in pages:
        lines = text.split("\n")
        m = _SHEET_HEADER.match(lines[0]) if lines else None
        if not m:
            continue
        name, first = m.group(1), int(m.group(2))
        sh = sheets.get(name)
        if sh is None:
            sh = sheets[name] = Sheet(name, first, [], [], [])
            order.append(name)
        sh.pages.append(page_no)
        body = [ln for ln in lines[1:] if ln != ""]
        # Row numbers inside a page are consecutive in the stored (blank-free) text; only
        # the page's first/last numbers are known, so use them as approximate anchors.
        last = int(m.group(3))
        start = last - len(body) + 1
        for i, ln in enumerate(body):
            sh.rows.append(ln.split("\t"))
            sh.row_numbers.append(max(start + i, first))
    return [sheets[n] for n in order]


def plan_sheet(sheet: Sheet) -> SheetPlan:
    header_idx, columns = _find_header(sheet.rows)
    if header_idx is None:
        return SheetPlan(sheet, None, "no recognizable header row")
    has_lines = any(f in columns for f in LINE_FIELDS)
    inv_col = columns.get("invoice_number")
    total_col = columns.get("total")
    if inv_col is None or total_col is None:
        return SheetPlan(sheet, None, "needs an invoice number column and a total column")
    total_header = _key(sheet.rows[header_idx][total_col])
    if has_lines and total_header not in INVOICE_TOTAL_KEYS:
        return SheetPlan(sheet, None, "total column looks like a line total")

    body = list(zip(sheet.row_numbers[header_idx + 1:], sheet.rows[header_idx + 1:]))
    invoices: dict[str, MappedInvoice] = {}
    for row_no, row in body:
        raw_inv = _cell(row, inv_col)
        if not raw_inv:
            continue  # blank invoice number: a subtotal/footer row, or a continuation we can't attribute
        inv = invoices.get(raw_inv)
        if inv is None:
            inv = invoices[raw_inv] = MappedInvoice(sheet.name, row_no, row_no)
        inv.row_end = row_no
        for fname, col in columns.items():
            if fname in LINE_FIELDS or fname == "line_total":
                continue
            raw = _cell(row, col)
            if raw and fname not in inv.fields:
                v = _convert(fname, raw)
                if v is not None:
                    inv.fields[fname] = MappedField(v, raw)
        if has_lines:
            item = {}
            for fname in (*LINE_FIELDS, "line_total"):
                col = columns.get(fname)
                raw = _cell(row, col) if col is not None else ""
                if raw:
                    v = raw if fname == "description" else parse_decimal_lenient(raw)
                    if v is not None:
                        item[fname] = MappedField(v, raw)
            if item:
                item["_row"] = MappedField(None, "\t".join(row))  # snippet: the row as stored
                inv.line_items.append(item)
    if not invoices:
        return SheetPlan(sheet, None, "no rows with an invoice number")
    return SheetPlan(sheet, list(invoices.values()))


# ---- helpers -------------------------------------------------------------------

def _find_header(rows: list[list[str]], scan: int = 20):
    best, best_cols = None, {}
    for i, row in enumerate(rows[:scan]):
        cols: dict[str, int] = {}
        for j, h in enumerate(row):
            k = _key(h)
            for fname, names in SYNONYMS.items():
                if k in names and fname not in cols:
                    cols[fname] = j
                    break
        if len(cols) >= 2 and len(cols) > len(best_cols):
            best, best_cols = i, cols
    return best, best_cols


def _cell(row: list[str], col: int | None) -> str:
    return row[col].strip() if col is not None and col < len(row) else ""


def _convert(fname: str, raw: str):
    if fname == "invoice_date":
        d = parse_date_strict(raw)
        return d.isoformat() if d else None
    if fname in ("subtotal", "tax", "total"):
        return parse_decimal_lenient(raw)
    if fname == "currency":
        c = raw.upper()
        return c if re.fullmatch(r"[A-Z]{3}", c) else None
    return raw
