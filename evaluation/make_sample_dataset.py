"""Build the SAMPLE labelled dataset in evaluation/labeled_dataset/.

    python evaluation/make_sample_dataset.py

Each file and its ground truth come from the same Python data below, so the answers are correct by
construction. This is a small synthetic set (invoices rendered by reportlab, one scanned copy, a few
spreadsheets) meant to show the format and to prove the evaluation runs end to end. It is NOT a
benchmark of real-world documents: for that, add your own 20-30 documents and their answers
(see evaluation/labeled_dataset/README.md).

Needs: reportlab, pymupdf, pillow, numpy, openpyxl (all in backend/requirements.txt or the sample-data script).
"""
import io
import json
import os
from decimal import Decimal

import numpy as np
import openpyxl
import pymupdf
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labeled_dataset")
D = Decimal
CENT = D("0.01")


def money(v: Decimal) -> str:
    return format(v.quantize(CENT), "f")


def make_invoice(number, date, supplier, customer, items, tax_rate="0.05", currency="USD", printed_total=None):
    """items: [(description, qty, unit_price)]. Returns the truth dict (what the printed page will say)."""
    lines = []
    for desc, qty, price in items:
        lines.append({"description": desc, "quantity": str(qty), "unit_price": money(D(price)),
                      "line_total": money(D(qty) * D(price))})
    subtotal = sum((D(i["line_total"]) for i in lines), D(0))
    tax = (subtotal * D(tax_rate)).quantize(CENT)
    total = D(printed_total) if printed_total is not None else subtotal + tax
    return {"invoice_number": number, "invoice_date": date, "supplier_name": supplier, "customer_name": customer,
            "subtotal": money(subtotal), "tax": money(tax), "total": money(total), "currency": currency,
            "line_items": lines}


def render_pdf(inv: dict) -> bytes:
    """Print exactly what the truth says; a field that is None is simply not printed."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    c.setFont("Helvetica-Bold", 20)
    c.drawString(60, y, "INVOICE")
    y -= 36
    c.setFont("Helvetica", 13)
    head = [f"Invoice number: {inv['invoice_number']}" if inv["invoice_number"] else None,
            f"Invoice date: {inv['invoice_date']}" if inv["invoice_date"] else None,
            f"Supplier: {inv['supplier_name']}" if inv["supplier_name"] else None,
            f"Customer: {inv['customer_name']}" if inv["customer_name"] else None]
    for line in [h for h in head if h] + [""]:
        c.drawString(60, y, line)
        y -= 22
    for it in inv["line_items"]:
        c.drawString(60, y, f"{it['description']:<28} {it['quantity']:>3} x {it['unit_price']:>8} "
                            f"= {it['line_total']:>9}")
        y -= 22
    y -= 10
    c.drawString(60, y, f"Subtotal: {inv['subtotal']}")
    y -= 22
    c.drawString(60, y, f"Tax (5%): {inv['tax']}")
    y -= 22
    c.setFont("Helvetica-Bold", 15)
    c.drawString(60, y, f"TOTAL: {inv['total']} {inv['currency']}")
    c.save()
    return buf.getvalue()


def scan_of(pdf: bytes, seed: int) -> Image.Image:
    """Rasterise the page, tilt it a little, add noise: a stand-in for a phone/scanner image."""
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img = img.rotate(1.5, expand=False, fillcolor=255, resample=Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32)
    arr = np.clip(arr * 0.92 + np.random.default_rng(seed).normal(0, 4, arr.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def sheet_invoice(number, date, supplier, total):
    """What a 4-column sheet (Invoice, Date, Supplier, Total) can tell us: the rest is null."""
    return {"invoice_number": number, "invoice_date": date, "supplier_name": supplier, "customer_name": None,
            "subtotal": None, "tax": None, "total": money(D(total)), "currency": None, "line_items": []}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    docs: dict[str, dict] = {}

    def add(name, data: bytes, invoices, expected_validation=(), expected_failure=False, notes=""):
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
        docs[name] = {"invoices": invoices, "expected_validation": list(expected_validation),
                      "expected_failure": expected_failure, "notes": notes}

    a = make_invoice("INV-2026-0042", "2026-11-24", "Acme Hosting Ltd", "Rahim Traders",
                     [("Web hosting (12 months)", 1, "240.00"), ("Domain renewal", 2, "15.50"),
                      ("Support hours", 10, "45.00")])
    b = make_invoice("B-7781", "2026-10-02", "Dhaka Office Supplies", "Karim Stores",
                     [("A4 paper (ream)", 20, "6.25"), ("Stapler", 3, "12.00")])
    c = make_invoice("C-0099", "2026-09-15", "Padma Logistics", "Meghna Foods",
                     [("Freight Dhaka-Khulna", 4, "310.00")])
    add("inv_clean_01.pdf", render_pdf(a), [a], notes="Digital PDF, every field present, 3 line items.")
    add("inv_clean_02.pdf", render_pdf(b), [b], notes="Digital PDF, 2 line items.")
    add("inv_clean_03.pdf", render_pdf(c), [c], notes="Digital PDF, 1 line item.")

    # The page itself is wrong: subtotal 100.00 + tax 5.00 but TOTAL printed as 150.00.
    w = make_invoice("W-1001", "2026-08-01", "Sylhet Tea Traders", "Rahim Traders",
                     [("Tea chest", 5, "20.00")], printed_total="150.00")
    add("inv_wrong_total.pdf", render_pdf(w), [w], ["TOTAL_MISMATCH"],
        notes="Total printed as 150.00; subtotal + tax is 105.00. The truth is what is printed.")

    # No date and no customer on the page: the right answer for both is null (anything else is invented).
    n = make_invoice("N-5520", None, "Chattogram Hardware", None, [("Hammer", 2, "9.50")])
    add("inv_no_date_no_customer.pdf", render_pdf(n), [n],
        notes="Date and customer are not printed anywhere. Truth is null for both.")

    # Same supplier and number as inv_clean_01, different date: the second upload is a duplicate.
    dup = dict(a, invoice_date="2026-11-20")
    add("inv_zz_duplicate_of_01.pdf", render_pdf(dup), [dup], ["DUPLICATE_INVOICE"],
        notes="Same invoice number and supplier as inv_clean_01.pdf; processed later (sorted by name).")

    # Scans (no text layer): the image is a rasterised copy of a clean invoice.
    img = scan_of(render_pdf(b2 := make_invoice("S-2201", "2026-07-20", "Barishal Boats Ltd", "Rahim Traders",
                                               [("Life jacket", 6, "18.00"), ("Rope (50m)", 2, "25.00")])), 1)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=60)
    add("scan_photo.jpg", buf.getvalue(), [b2], notes="JPEG, 1.5 degree tilt and noise: needs OCR.")
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        b3 = make_invoice("S-2202", "2026-07-21", "Barishal Boats Ltd", "Meghna Foods", [("Anchor", 1, "140.00")])
        buf = io.BytesIO()
        scan_of(render_pdf(b3), 2).save(buf, "JPEG", quality=60)
        page.insert_image(page.rect, stream=buf.getvalue())
        add("scan_image_only.pdf", doc.tobytes(), [b3], notes="PDF holding only an image: needs OCR.")

    # Spreadsheets
    s1 = [sheet_invoice("INV-101", "2026-01-05", "Acme Hosting Ltd", "1250.50"),
          sheet_invoice("INV-102", "2026-02-11", "Acme Hosting Ltd", "310.00")]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(["Invoice", "Date", "Supplier", "Total"])
    ws.append(["INV-101", "2026-01-05", "Acme Hosting Ltd", 1250.5])
    ws.append(["INV-102", "2026-02-11", "Acme Hosting Ltd", 310])
    wb.create_sheet("Empty")
    bio = io.BytesIO()
    wb.save(bio)
    add("sheet_two_invoices.xlsx", bio.getvalue(), s1,
        notes="Header row Invoice/Date/Supplier/Total; only those four fields can be known. Plus an empty sheet.")

    rows = ["Rechnung;Datum;Lieferant;Betrag"]
    s2 = []
    for i, amount in enumerate(["100,50", "2301,50", "3998,00", "45,25"], start=1):
        rows.append(f"R-{i:03d};2026-03-{i:02d};Müller & Söhne GmbH;{amount}")
        s2.append(sheet_invoice(f"R-{i:03d}", f"2026-03-{i:02d}", "Müller & Söhne GmbH", amount.replace(",", ".")))
    add("sheet_german_semicolon.csv", ("\r\n".join(rows) + "\r\n").encode("cp1252"), s2,
        notes="German headers, semicolons, decimal comma, Windows-1252 encoding.")

    # A file that cannot be read: the system must fail cleanly, not invent a result.
    add("corrupt.pdf", b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 99 0 R >>\n" + bytes(range(200)), [],
        expected_failure=True, notes="Broken PDF structure: the right outcome is status 'failed'.")

    truth = {"format_version": 1,
             "description": "SAMPLE synthetic set made by evaluation/make_sample_dataset.py. Replace with your own.",
             "documents": dict(sorted(docs.items()))}
    with open(os.path.join(OUT, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(docs)} documents to {OUT}")


if __name__ == "__main__":
    main()
