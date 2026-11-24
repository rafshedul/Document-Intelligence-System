from app.services.invoice_segmenter import find_invoice_number, has_header, split_invoices


def inv(n, extra=""):
    return f"INVOICE\nInvoice No: INV-{n:04d}\nSupplier: Acme\nItem 1   10.00\nTotal: 10.00 {extra}"


def test_find_invoice_number():
    assert find_invoice_number("Invoice No: INV-2026-0042") == "INV-2026-0042"
    assert find_invoice_number("Invoice # 5531\nDate: 2026-01-01") == "5531"
    assert find_invoice_number("Invoice date: 2026-01-01") is None  # 'date' is not a number
    assert find_invoice_number("nothing here") is None


def test_has_header():
    assert has_header("TAX INVOICE\nAcme")
    assert not has_header("Dear customer,\nplease find attached")


def test_one_segment_per_invoice_number():
    segs = split_invoices([(i, inv(i)) for i in range(1, 6)])
    assert [(s.page_start, s.page_end) for s in segs] == [(i, i) for i in range(1, 6)]


def test_multi_page_invoice_stays_together():
    pages = [(1, "INVOICE\nInvoice No: INV-0001\nItem A 5.00"), (2, "Item B 5.00\nPage 2 of 2\nTotal: 10.00"),
             (3, inv(2))]
    segs = split_invoices(pages)
    assert [(s.page_start, s.page_end) for s in segs] == [(1, 2), (3, 3)]
    assert "Item B" in segs[0].text


def test_same_number_repeated_in_page_header_is_one_invoice():
    pages = [(1, "Invoice No: INV-0001\nItem A"), (2, "Invoice No: INV-0001\nItem B\nTotal: 9.00")]
    assert len(split_invoices(pages)) == 1


def test_headers_without_numbers_split_after_a_total():
    pages = [(1, "INVOICE\nAcme\nTotal: 10.00"), (2, "INVOICE\nBeta Co\nTotal: 20.00")]
    assert len(split_invoices(pages)) == 2


def test_continued_page_with_header_is_not_a_new_invoice():
    pages = [(1, "INVOICE\nAcme\nTotal: 10.00"), (2, "INVOICE (continued)\nmore rows")]
    assert len(split_invoices(pages)) == 1


def test_blank_pages_are_skipped_and_number_found_late():
    pages = [(1, "  \n"), (2, "Acme\nItem"), (3, "Invoice No: INV-0007\nTotal: 1.00")]
    segs = split_invoices(pages)
    assert len(segs) == 1 and segs[0].invoice_hint == "INV-0007" and segs[0].page_start == 2


def test_empty_input():
    assert split_invoices([]) == [] and split_invoices([(1, "")]) == []


def test_thousand_pages_one_invoice_each():
    segs = split_invoices([(i, inv(i)) for i in range(1, 1001)])
    assert len(segs) == 1000
