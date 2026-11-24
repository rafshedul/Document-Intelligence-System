"""Regenerate the small sample files in sample_data/.

    python scripts/generate_sample_data.py
Needs: reportlab, pymupdf, pillow, numpy, openpyxl.
"""
import io
import os
import random

import numpy as np
import openpyxl
import pymupdf
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = os.path.join(os.path.dirname(__file__), "..", "sample_data")

LINES = [("Web hosting (12 months)", 1, "240.00"), ("Domain renewal", 2, "15.50"), ("Support hours", 10, "45.00")]


def invoice_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    c.setFont("Helvetica-Bold", 20); c.drawString(60, y, "INVOICE"); y -= 36
    c.setFont("Helvetica", 13)
    for line in ["Invoice number: INV-2026-0042", "Invoice date: 2026-11-24",
                 "Supplier: Acme Hosting Ltd", "Customer: Rahim Traders", ""]:
        c.drawString(60, y, line); y -= 22
    total = 0.0
    for desc, qty, price in LINES:
        amount = qty * float(price); total += amount
        c.drawString(60, y, f"{desc:<28} {qty:>3} x {price:>8} = {amount:>9.2f}"); y -= 22
    y -= 10
    c.drawString(60, y, f"Subtotal: {total:.2f}"); y -= 22
    c.drawString(60, y, f"Tax (5%): {total * 0.05:.2f}"); y -= 22
    c.setFont("Helvetica-Bold", 15)
    c.drawString(60, y, f"TOTAL: {total * 1.05:.2f} USD")
    c.save()
    return buf.getvalue()


def scan_image() -> Image.Image:
    """Render the invoice to pixels, tilt it a little and add noise, like a real scan."""
    with pymupdf.open(stream=invoice_pdf_bytes(), filetype="pdf") as doc:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img = img.rotate(1.5, expand=False, fillcolor=255, resample=Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32)
    rng = np.random.default_rng(1)
    arr = np.clip(arr * 0.92 + rng.normal(0, 4, arr.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/digital_invoice.pdf", "wb") as f:
        f.write(invoice_pdf_bytes())

    img = scan_image()
    img.save(f"{OUT}/scanned_invoice.jpg", quality=60)
    # Image-only PDF: no text layer, so the pipeline must OCR it.
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=60)
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(f"{OUT}/scanned_invoice.pdf")

    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Invoices"
    ws.append(["Invoice", "Date", "Supplier", "Total"])
    ws.append(["INV-001", "2026-01-05", "Acme Hosting Ltd", 1250.5])
    ws.append(["INV-002", "2026-02-11", "Acme Hosting Ltd", 310])
    ws2 = wb.create_sheet("LineItems")
    ws2["A1"] = "Q1 line items"; ws2.merge_cells("A1:C1")
    ws2.append([]); ws2.append(["Description", "Qty", "Price"])
    for d, q, p in LINES:
        ws2.append([d, q, float(p)])
    wb.create_sheet("Empty")
    wb.save(f"{OUT}/multi_sheet.xlsx")

    rows = ["Rechnung;Datum;Lieferant;Betrag"]
    for i in range(1, 16):
        rows.append(f"R-{i:03d};2026-03-{i:02d};Müller & Söhne GmbH;{random.Random(i).randint(100, 9999)},50")
    with open(f"{OUT}/invoices.csv", "w", encoding="cp1252", newline="") as f:
        f.write("\r\n".join(rows) + "\r\n")

    with open(f"{OUT}/corrupted.pdf", "wb") as f:
        f.write(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog /Pages 99 0 R >>\n" + os.urandom(200))


if __name__ == "__main__":
    main()
