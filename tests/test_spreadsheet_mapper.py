from conftest import collect, sample

from app.services import spreadsheet_mapper as sm
from app.services import spreadsheet_processor


def pages_of(path):
    return [(p.page_number, p.text) for p in collect(spreadsheet_processor.process(path))]


def test_csv_with_translated_headers_is_mapped():
    sheets = sm.rebuild_sheets(pages_of(sample("invoices.csv")))  # Rechnung;Datum;Lieferant;Betrag
    plan = sm.plan_sheet(sheets[0])
    assert plan.invoices is not None and len(plan.invoices) == 15
    first = plan.invoices[0]
    assert first.fields["invoice_number"].value == "R-001"
    assert first.fields["supplier_name"].value == "Müller & Söhne GmbH"
    assert first.fields["invoice_date"].value == "2026-03-01"
    assert first.fields["total"].snippet  # raw cell kept as source snippet


def test_multi_sheet_workbook_maps_one_sheet_and_sends_other_to_llm():
    plans = [sm.plan_sheet(s) for s in sm.rebuild_sheets(pages_of(sample("multi_sheet.xlsx")))]
    assert [p.sheet.name for p in plans] == ["Invoices", "LineItems"]
    assert len(plans[0].invoices) == 2 and plans[0].invoices[0].fields["total"].value == 1250.5
    assert plans[1].invoices is None  # no invoice number column


def page(rows, name="S"):
    return [(1, f"# Sheet: {name} (rows 1-{len(rows)})\n" + "\n".join("\t".join(r) for r in rows) + "\n")]


def plan(rows):
    return sm.plan_sheet(sm.rebuild_sheets(page(rows))[0])


def test_line_item_sheet_groups_rows_by_invoice():
    p = plan([["Invoice No", "Description", "Qty", "Unit Price", "Line Total", "Invoice Total"],
              ["A-1", "Hosting", "1", "240.00", "240.00", "255.50"],
              ["A-1", "Domain", "2", "7.75", "15.50", "255.50"],
              ["B-2", "Support", "10", "45.00", "450.00", "450.00"]])
    assert [len(i.line_items) for i in p.invoices] == [2, 1]
    assert p.invoices[0].fields["total"].value == 255.5 or str(p.invoices[0].fields["total"].value) == "255.50"
    assert p.invoices[0].line_items[1]["quantity"].value == 2


def test_bare_total_in_line_item_sheet_is_not_used_as_invoice_total():
    p = plan([["Invoice No", "Description", "Qty", "Total"], ["A-1", "Hosting", "1", "240.00"]])
    assert p.invoices is None and "line total" in p.reason


def test_missing_columns_fall_back_to_llm():
    assert plan([["Foo", "Bar"], ["1", "2"]]).invoices is None
    assert plan([["Invoice No", "Date"], ["A-1", "2026-01-01"]]).invoices is None  # no total column


def test_ambiguous_date_and_amount_become_missing_not_guessed():
    p = plan([["Invoice", "Date", "Total"], ["A-1", "03/04/2026", "1,250"]])
    assert "invoice_date" not in p.invoices[0].fields and "total" not in p.invoices[0].fields


def test_rows_without_invoice_number_are_skipped():
    p = plan([["Invoice", "Total"], ["A-1", "10.00"], ["", "Subtotal 10.00"]])
    assert len(p.invoices) == 1
