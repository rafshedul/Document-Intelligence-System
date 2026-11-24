import pymupdf
import pytest
from conftest import collect, needs_tesseract, sample

from app.core.config import settings
from app.models.enums import ExtractionMethod
from app.services import pdf_processor
from app.services.exceptions import CorruptedFileError, EmptyFileError, PasswordProtectedError

LONG = "This page has plenty of digital text, well over fifty characters in total."


def make_pdf(path, pages=1, text=LONG):
    with pymupdf.open() as doc:
        for i in range(pages):
            doc.new_page().insert_text((72, 72), f"{text} #{i + 1}")
        doc.save(str(path))
    return str(path)


def test_digital_pdf_text():
    pages = collect(pdf_processor.process(sample("digital_invoice.pdf")))
    assert len(pages) == 1
    assert pages[0].method == ExtractionMethod.digital and pages[0].ocr_confidence is None
    assert "INV-2026-0042" in pages[0].text


def test_pages_are_batched(tmp_path):
    ex = pdf_processor.process(make_pdf(tmp_path / "7.pdf", pages=7), batch_size=3)
    sizes = [len(b) for b in ex.batches]
    assert sizes == [3, 3, 1]
    assert ex.info["page_count"] == 7 and ex.info["digital_pages"] == 7


def test_page_numbers_are_sequential(tmp_path):
    pages = collect(pdf_processor.process(make_pdf(tmp_path / "5.pdf", pages=5), batch_size=2))
    assert [p.page_number for p in pages] == [1, 2, 3, 4, 5]


@needs_tesseract
def test_scanned_pdf_goes_to_ocr():
    pages = collect(pdf_processor.process(sample("scanned_invoice.pdf")))
    assert pages[0].method == ExtractionMethod.ocr
    assert pages[0].ocr_confidence > 80
    assert "INV-2026-0042" in pages[0].text


@needs_tesseract
def test_mixed_pdf_only_scanned_pages_use_ocr(tmp_path):
    with pymupdf.open(sample("scanned_invoice.pdf")) as scan, pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), LONG)
        doc.insert_pdf(scan)
        doc.save(str(tmp_path / "mixed.pdf"))
    pages = collect(pdf_processor.process(str(tmp_path / "mixed.pdf")))
    assert [p.method for p in pages] == [ExtractionMethod.digital, ExtractionMethod.ocr]


def test_threshold_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pdf_min_text_chars", 5)
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "short text")  # 10 chars: digital under a 5-char threshold
        doc.save(str(tmp_path / "s.pdf"))
    assert collect(pdf_processor.process(str(tmp_path / "s.pdf")))[0].method == ExtractionMethod.digital


def test_password_protected_pdf(tmp_path):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), LONG)
        doc.save(str(tmp_path / "locked.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="o")
    with pytest.raises(PasswordProtectedError):
        pdf_processor.process(str(tmp_path / "locked.pdf"))


def test_corrupted_pdf():
    with pytest.raises((CorruptedFileError, EmptyFileError)):
        collect(pdf_processor.process(sample("corrupted.pdf")))


def test_truncated_pdf_does_not_crash(tmp_path):
    data = open(sample("digital_invoice.pdf"), "rb").read()
    p = tmp_path / "cut.pdf"
    p.write_bytes(data[: len(data) // 2])
    try:
        collect(pdf_processor.process(str(p)))  # MuPDF may repair it
    except (CorruptedFileError, EmptyFileError):
        pass
