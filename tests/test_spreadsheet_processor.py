import csv

import openpyxl
import pytest
import xlwt
from conftest import collect, sample

from app.core.config import settings
from app.models.enums import ExtractionMethod
from app.services import spreadsheet_processor as sp
from app.services.exceptions import CorruptedFileError, EmptyFileError


def test_xlsx_sheets_read_separately_and_empty_skipped():
    ex = sp.process(sample("multi_sheet.xlsx"))
    pages = collect(ex)
    assert len(pages) == 2
    assert pages[0].text.startswith("# Sheet: Invoices")
    assert "INV-001\t2026-01-05\tAcme Hosting Ltd\t1250.5" in pages[0].text
    assert "INV-002\t2026-02-11\tAcme Hosting Ltd\t310\n" in pages[0].text  # 310.0 -> 310
    assert pages[1].text.startswith("# Sheet: LineItems")
    assert ex.info["skipped_empty_sheets"] == ["Empty"]
    assert all(p.method == ExtractionMethod.spreadsheet for p in pages)


def test_xlsx_merged_cells_are_filled():
    text = collect(sp.process(sample("multi_sheet.xlsx")))[1].text
    assert "Q1 line items\tQ1 line items\tQ1 line items" in text


def test_xls_with_merged_cells_and_empty_sheet(tmp_path):
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Data")
    ws.write_merge(0, 0, 0, 2, "Title")
    ws.write(1, 0, "a"); ws.write(1, 1, 2); ws.write(1, 2, 3.5)
    wb.add_sheet("Blank")
    p = tmp_path / "old.xls"
    wb.save(str(p))
    ex = sp.process(str(p))
    pages = collect(ex)
    assert len(pages) == 1
    assert "Title\tTitle\tTitle" in pages[0].text and "a\t2\t3.5" in pages[0].text
    assert ex.info["skipped_empty_sheets"] == ["Blank"]


def test_csv_encoding_and_delimiter_detected():
    pages = collect(sp.process(sample("invoices.csv")))  # cp1252, ';'
    assert "Müller & Söhne GmbH" in pages[0].text
    first = pages[0].text.splitlines()[1]
    assert first == "Rechnung\tDatum\tLieferant\tBetrag"


@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_csv_delimiters(tmp_path, delim):
    p = tmp_path / "d.csv"
    p.write_text(delim.join(["a", "b", "c"]) + "\n" + delim.join(["1", "2", "3"]) + "\n")
    assert "a\tb\tc\n1\t2\t3" in collect(sp.process(str(p)))[0].text


def test_csv_utf8_bom_and_quoted_delimiters(tmp_path):
    p = tmp_path / "b.csv"
    p.write_bytes("﻿name,note\nAnna,\"hello, world\"\n".encode("utf-8"))
    text = collect(sp.process(str(p)))[0].text
    assert "name\tnote" in text and "Anna\thello, world" in text


def test_csv_utf16(tmp_path):
    p = tmp_path / "u.csv"
    p.write_bytes("a,b\n১,২\n".encode("utf-16"))
    assert "১\t২" in collect(sp.process(str(p)))[0].text


def test_csv_ragged_rows_and_blank_rows(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("a,b,c\n\n1,2\n,,\n3,4,5,6\n")
    text = collect(sp.process(str(p)))[0].text
    assert "1\t2" in text and "3\t4\t5\t6" in text
    assert "(rows 1-5)" in text


def test_large_sheet_is_split_into_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "spreadsheet_rows_per_page", 4)
    p = tmp_path / "big.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        for i in range(10):
            w.writerow([i, "x"])
    pages = collect(sp.process(str(p)))
    assert [x.page_number for x in pages] == [1, 2, 3]


def test_empty_csv_and_all_empty_workbook(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("  \n\n")
    with pytest.raises(EmptyFileError):
        sp.process(str(p))
    wb = openpyxl.Workbook()
    wb.save(tmp_path / "e.xlsx")
    with pytest.raises(EmptyFileError):
        sp.process(str(tmp_path / "e.xlsx"))


def test_corrupted_workbooks(tmp_path):
    p = tmp_path / "bad.xlsx"
    p.write_bytes(b"PK\x03\x04" + b"junk" * 50)
    with pytest.raises(CorruptedFileError):
        sp.process(str(p))
    p = tmp_path / "bad.xls"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"junk" * 50)
    with pytest.raises(CorruptedFileError):
        sp.process(str(p))


# ---- more encodings, delimiters, XLS and broken workbooks ---------------------------------------------------------------

ROWS = [("Rechnung", "Lieferant", "Betrag")] + [(f"R-{i:03d}", name, f"{i * 10},50") for i, name in
                                                  enumerate(["Müller & Söhne", "Café Zürich", "Ångström AB", "Søren Ørsted A/S"] * 5, 1)]


def csv_bytes(rows, delim=";", encoding="utf-8"):
    return "\r\n".join(delim.join(r) for r in rows).encode(encoding)


@pytest.mark.parametrize("encoding,text,expected", [
    ("utf-8", "Müller & Söhne", "Müller & Söhne"),
    ("utf-8-sig", "Café Zürich", "Café Zürich"),
    ("latin-1", "Müller & Söhne", "Müller & Söhne"),
    ("cp1252", "Preis in € netto", "Preis in € netto"),
    ("utf-16", "Ångström AB", "Ångström AB"),
    ("utf-16-be", "Zürich", "Zürich"),
    ("cp1251", "Москва и область", "Москва и область"),
    ("utf-8", "চালান নম্বর ও গ্রাহক", "চালান নম্বর ও গ্রাহক"),
    ("utf-8", "東京都の請求書", "東京都の請求書"),
    ("shift_jis", "東京都の請求書です", "東京都の請求書です"),
])
def test_csv_encodings_are_detected(tmp_path, encoding, text, expected):
    rows = [("id", "text", "n")] + [(str(i), text, str(i)) for i in range(25)]
    p = tmp_path / "e.csv"
    p.write_bytes(csv_bytes(rows, ",", encoding) if encoding != "utf-16-be" else b"\xfe\xff" + csv_bytes(rows, ",", encoding))
    got = collect(sp.process(str(p)))[0].text
    assert expected in got and "�" not in got  # no replacement characters


def test_semicolon_csv_with_decimal_commas(tmp_path):
    p = tmp_path / "de.csv"
    p.write_bytes(csv_bytes(ROWS, ";", "cp1252"))
    text = collect(sp.process(str(p)))[0].text
    assert "R-001\tMüller & Söhne\t10,50" in text and "R-020" in text  # the comma stays inside the value


def test_csv_with_a_delimiter_inside_quotes(tmp_path):
    p = tmp_path / "q.csv"
    p.write_text('id;name;note\n1;"Smith; John";"line 1\nline 2"\n', encoding="utf-8")
    text = collect(sp.process(str(p)))[0].text
    assert "Smith; John" in text and "line 1 line 2" in text


def test_large_csv_is_streamed_in_pages(tmp_path):
    p = tmp_path / "big.csv"
    with open(p, "w") as f:
        f.write("id,amount\n")
        f.writelines(f"{i},{i}.50\n" for i in range(5000))
    ex = sp.process(str(p))
    pages = collect(ex)
    assert len(pages) == 6 and [x.page_number for x in pages] == [1, 2, 3, 4, 5, 6]  # 5001 rows / 1000 per page
    assert "4999\t4999.50" in pages[-1].text


def test_old_xls_with_dates_numbers_and_several_sheets(tmp_path):
    import datetime as dt

    wb = xlwt.Workbook()
    a = wb.add_sheet("Invoices")
    a.write(0, 0, "No"); a.write(0, 1, "Date"); a.write(0, 2, "Total")
    style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    a.write(1, 0, "INV-9"); a.write(1, 1, dt.datetime(2026, 3, 4), style); a.write(1, 2, 1234.5)
    b = wb.add_sheet("Notes")
    b.write(0, 0, "hello")
    wb.add_sheet("Empty")
    p = tmp_path / "old.xls"
    wb.save(str(p))
    ex = sp.process(str(p))
    pages = collect(ex)
    assert [x.text.splitlines()[0].split(" (")[0] for x in pages] == ["# Sheet: Invoices", "# Sheet: Notes"]
    assert "INV-9\t2026-03-04\t1234.5" in pages[0].text and ex.info["skipped_empty_sheets"] == ["Empty"]


def test_xls_that_is_only_empty_sheets(tmp_path):
    wb = xlwt.Workbook()
    wb.add_sheet("A"); wb.add_sheet("B")
    p = tmp_path / "empty.xls"
    wb.save(str(p))
    with pytest.raises(EmptyFileError):
        sp.process(str(p))


def test_xlsx_with_a_vertical_merge_and_hidden_whitespace(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["Group", "Item"])
    ws.append(["Hardware", "Cable"])
    ws.append([None, "Adapter"])
    ws.append(["   ", "  "])  # whitespace only: counts as empty
    ws.merge_cells("A2:A3")
    p = tmp_path / "v.xlsx"
    wb.save(p)
    text = collect(sp.process(str(p)))[0].text
    assert "Hardware\tCable" in text and "Hardware\tAdapter" in text  # merged value copied down
    assert text.splitlines()[1:] == ["Group\tItem", "Hardware\tCable", "Hardware\tAdapter"]  # the blank row was dropped


def test_a_truncated_workbook_is_reported_as_corrupted(tmp_path):
    data = open(sample("multi_sheet.xlsx"), "rb").read()
    for cut in (len(data) // 2, len(data) - 40):
        p = tmp_path / f"cut{cut}.xlsx"
        p.write_bytes(data[:cut])
        with pytest.raises(CorruptedFileError):
            sp.process(str(p))


def test_a_workbook_with_a_broken_inner_file_is_reported_as_corrupted(tmp_path):
    import zipfile

    src = tmp_path / "ok.xlsx"
    src.write_bytes(open(sample("multi_sheet.xlsx"), "rb").read())
    bad = tmp_path / "bad.xlsx"
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            zout.writestr(item, b"<not xml" if item.filename == "xl/worksheets/sheet1.xml" else data)
    with pytest.raises(CorruptedFileError):
        collect(sp.process(str(bad)))
