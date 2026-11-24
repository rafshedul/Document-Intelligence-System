"""XLSX (openpyxl), XLS (xlrd) and CSV (chardet + csv sniffing) -> text pages.

Each sheet is read separately; empty sheets are skipped. A sheet is split into
pages of `spreadsheet_rows_per_page` rows, so big sheets don't become one
giant text blob. Page text is tab-separated and starts with a `# Sheet:` line.
"""
import csv
import datetime as dt
import io
import zipfile
from typing import Iterable, Iterator

import chardet
import openpyxl
import pandas as pd
import xlrd

from app.core.config import settings
from app.models.enums import ExtractionMethod
from app.services.exceptions import (
    CorruptedFileError,
    EmptyFileError,
    PasswordProtectedError,
)
from app.services.types import Extraction, PageResult

CSV_DELIMITERS = ",;\t|"


def process(path: str, batch_size: int | None = None) -> Extraction:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"PK"):
        sheets = _xlsx_sheets(path)
    elif head.startswith(b"\xd0\xcf\x11\xe0"):
        sheets = _xls_sheets(path)
    else:
        sheets = _csv_sheets(path)

    info = {"page_count": 0, "sheets": [], "skipped_empty_sheets": []}
    # Peek so an entirely empty workbook fails now rather than mid-iteration.
    batches = _paginate(sheets, info)
    first = next(batches, None)
    if first is None:
        raise EmptyFileError("The file contains no data (every sheet is empty).")

    def gen():
        yield first
        yield from batches

    return Extraction(gen(), info)


# ---- shared ----------------------------------------------------------------

def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _paginate(sheets: Iterable[tuple[str, Iterator[list]]], info: dict) -> Iterator[list[PageResult]]:
    """sheets: (name, rows). Yields one batch per sheet-page group."""
    page_no = 0
    per_page = settings.spreadsheet_rows_per_page
    for name, rows in sheets:
        buffer: list[tuple[int, list[str]]] = []
        emitted = 0

        def flush():
            nonlocal page_no, emitted
            df = pd.DataFrame([r for _, r in buffer]).fillna("")
            body = df.to_csv(sep="\t", index=False, header=False, lineterminator="\n")
            page_no += 1
            emitted += 1
            header = f"# Sheet: {name} (rows {buffer[0][0]}-{buffer[-1][0]})\n"
            page = PageResult(page_no, header + body, ExtractionMethod.spreadsheet)
            buffer.clear()
            return page

        for row_no, row in enumerate(rows, start=1):
            cells = [_fmt(v) for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue  # blank row
            buffer.append((row_no, cells))
            if len(buffer) >= per_page:
                yield [flush()]
        if buffer:
            yield [flush()]
        if emitted:
            info["sheets"].append(name)
            info["page_count"] = page_no
        else:
            info["skipped_empty_sheets"].append(name)


# ---- XLSX ------------------------------------------------------------------

def _xlsx_sheets(path: str):
    try:
        # Pass a file object: openpyxl rejects paths without a .xlsx extension,
        # and stored files have generated names.
        with open(path, "rb") as fh:
            wb = openpyxl.load_workbook(fh, data_only=True)  # cached formula values
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError("The Excel file is damaged.") from exc
    except Exception as exc:
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            raise PasswordProtectedError("The spreadsheet is password-protected.") from exc
        raise CorruptedFileError(f"The Excel file cannot be read: {str(exc)[:150]}") from exc

    def gen():
        try:
            for ws in wb.worksheets:
                _fill_merged_xlsx(ws)
                yield ws.title, ws.iter_rows(values_only=True)
        finally:
            wb.close()

    return gen()


def _fill_merged_xlsx(ws) -> None:
    """Copy the top-left value of each merged range into every cell of the range."""
    for rng in list(ws.merged_cells.ranges):
        value = ws.cell(rng.min_row, rng.min_col).value
        ws.unmerge_cells(str(rng))
        for row in ws.iter_rows(rng.min_row, rng.max_row, rng.min_col, rng.max_col):
            for cell in row:
                cell.value = value


# ---- XLS -------------------------------------------------------------------

def _xls_sheets(path: str):
    try:
        try:
            book = xlrd.open_workbook(path, formatting_info=True)  # needed for merged cells
        except NotImplementedError:
            book = xlrd.open_workbook(path)
    except xlrd.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise PasswordProtectedError("The spreadsheet is password-protected.") from exc
        raise CorruptedFileError(f"The XLS file cannot be read: {str(exc)[:150]}") from exc
    except Exception as exc:
        raise CorruptedFileError("The XLS file is damaged.") from exc

    def sheet_rows(sh):
        grid = [
            [_xls_value(sh.cell(r, c), book.datemode) for c in range(sh.ncols)]
            for r in range(sh.nrows)
        ]
        for rlo, rhi, clo, chi in getattr(sh, "merged_cells", []):
            value = grid[rlo][clo]
            for r in range(rlo, rhi):
                for c in range(clo, chi):
                    grid[r][c] = value
        return iter(grid)

    def gen():
        try:
            for sh in book.sheets():
                yield sh.name, sheet_rows(sh)
        finally:
            book.release_resources()

    return gen()


def _xls_value(cell, datemode):
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except Exception:
            return cell.value
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return None
    return cell.value


# ---- CSV -------------------------------------------------------------------

def detect_encoding(sample: bytes) -> str:
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        sample.decode("utf-8")
        return "utf-8"  # chardet is unreliable on short pure-ASCII/UTF-8 samples
    except UnicodeDecodeError as exc:
        # A multi-byte char cut at the sample end is not evidence against UTF-8.
        if exc.start >= len(sample) - 3:
            return "utf-8"
    guess = chardet.detect(sample)
    enc = guess.get("encoding")
    if not enc or (guess.get("confidence") or 0) < 0.3:
        return "cp1252"  # most common legacy encoding for exported CSVs
    return enc


def detect_delimiter(text_sample: str) -> str:
    try:
        return csv.Sniffer().sniff(text_sample, delimiters=CSV_DELIMITERS).delimiter
    except csv.Error:
        counts = {d: text_sample.count(d) for d in CSV_DELIMITERS}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def _csv_sheets(path: str):
    with open(path, "rb") as f:
        sample = f.read(256 * 1024)
    if not sample.strip():
        raise EmptyFileError("The CSV file contains only whitespace.")
    encoding = detect_encoding(sample)
    try:
        text_sample = sample.decode(encoding, errors="replace")
    except LookupError:
        encoding, text_sample = "cp1252", sample.decode("cp1252", errors="replace")
    # Drop a possibly cut last line so the sniffer sees whole rows.
    delimiter = detect_delimiter(text_sample.rsplit("\n", 1)[0] if "\n" in text_sample else text_sample)

    def gen():
        csv.field_size_limit(10 * 1024 * 1024)
        with open(path, "r", encoding=encoding, errors="replace", newline="") as f:
            yield "csv", csv.reader(f, delimiter=delimiter)

    return gen()
