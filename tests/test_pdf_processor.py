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


# ---- more PDF shapes ----------------------------------------------------------------------------------------------

def test_pdf_with_a_blank_page_does_not_crash(tmp_path):
    with pymupdf.open() as doc:
        doc.new_page()  # nothing on it
        doc.new_page().insert_text((72, 72), LONG)
        doc.save(str(tmp_path / "blank.pdf"))
    pages = collect(pdf_processor.process(str(tmp_path / "blank.pdf")))
    assert pages[0].text.strip() == "" and pages[0].error is None  # empty page: empty text, no invented words
    assert pages[1].method == ExtractionMethod.digital and "digital text" in pages[1].text


def test_pdf_with_no_pages_is_an_empty_file_error(tmp_path):
    p = tmp_path / "zero.pdf"
    p.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
                  b"trailer<</Root 1 0 R/Size 3>>\n%%EOF\n")
    with pytest.raises((EmptyFileError, CorruptedFileError)):
        collect(pdf_processor.process(str(p)))


def test_zero_byte_pdf_is_rejected_before_it_reaches_the_processor(tmp_path):
    from app.services import file_router

    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    with pytest.raises(EmptyFileError):
        file_router.detect(str(p))


def test_many_pages_are_processed_in_batches_without_loading_everything(tmp_path):
    ex = pdf_processor.process(make_pdf(tmp_path / "120.pdf", pages=120), batch_size=25)
    sizes = []
    for batch in ex.batches:  # consumed lazily, batch by batch
        sizes.append(len(batch))
        assert len(batch) <= 25
    assert sizes == [25, 25, 25, 25, 20] and ex.info["digital_pages"] == 120


def test_a_page_that_fails_is_reported_and_the_rest_continue(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "5.pdf", pages=5)
    orig_load = pymupdf.Document.load_page

    def load(self, i):
        if i == 2:
            raise RuntimeError("page 3 is damaged")
        return orig_load(self, i)

    monkeypatch.setattr(pymupdf.Document, "load_page", load)
    ex = pdf_processor.process(path)
    pages = collect(ex)
    assert [p.error is None for p in pages] == [True, True, False, True, True]
    assert "damaged" in pages[2].error and ex.info["failed_pages"] == [{"page": 3, "error": "page 3 is damaged"}]


@needs_tesseract
def test_scanned_pdf_with_an_upside_down_page_is_read(tmp_path):
    from PIL import Image
    import io

    img = Image.open(sample("scanned_invoice.jpg")).convert("L").rotate(180)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    with pymupdf.open() as doc:
        doc.new_page(width=595, height=842).insert_image(pymupdf.Rect(0, 0, 595, 842), stream=buf.getvalue())
        doc.save(str(tmp_path / "flipped.pdf"))
    p = collect(pdf_processor.process(str(tmp_path / "flipped.pdf")))[0]
    assert p.method == ExtractionMethod.ocr and "INV-2026-0042" in p.text and p.ocr_confidence > 85


def test_a_poster_sized_page_is_rendered_within_the_pixel_budget(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ocr_max_pixels", 2_000_000)
    with pymupdf.open() as doc:
        page = doc.new_page(width=3000, height=3000)  # 9M points -> ~150M pixels at 300 dpi
        arr = pdf_processor._render_gray(page)
    assert arr.shape[0] * arr.shape[1] <= 2_000_000 * 1.02 and arr.ndim == 2


def test_embedded_text_wins_when_ocr_finds_less(tmp_path, monkeypatch):
    from app.services import ocr_processor
    from app.services.ocr_processor import OcrResult

    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "tiny note")  # under 50 characters: goes to OCR first
        doc.save(str(tmp_path / "t.pdf"))
    monkeypatch.setattr(ocr_processor, "ocr_array", lambda *a, **k: OcrResult("", 0.0))  # OCR reads nothing
    p = collect(pdf_processor.process(str(tmp_path / "t.pdf")))[0]
    assert p.method == ExtractionMethod.digital and p.text.strip() == "tiny note"


def test_missing_tesseract_stops_a_scanned_pdf_instead_of_producing_empty_pages(monkeypatch):
    from app.services import ocr_processor

    def gone(*a, **k):
        raise ocr_processor.OCRUnavailableError("Tesseract OCR is not installed on this server.")

    monkeypatch.setattr(ocr_processor, "ocr_array", gone)
    with pytest.raises(ocr_processor.OCRUnavailableError):
        collect(pdf_processor.process(sample("scanned_invoice.pdf")))  # whole job fails clearly; no silent empty pages
