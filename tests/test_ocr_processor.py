import numpy as np
import pytesseract
import pytest
from conftest import collect, needs_tesseract, sample
from PIL import Image

from app.services import ocr_processor
from app.services.exceptions import CorruptedFileError, OCRError, OCRUnavailableError


def gray_sample() -> np.ndarray:
    return np.asarray(Image.open(sample("scanned_invoice.jpg")).convert("L"))


@needs_tesseract
def test_image_ocr_text_and_confidence():
    pages = collect(ocr_processor.process(sample("scanned_invoice.jpg")))
    assert len(pages) == 1
    assert "INV-2026-0042" in pages[0].text and "757.05" in pages[0].text
    assert 80 < pages[0].ocr_confidence <= 100


@needs_tesseract
def test_multipage_tiff(tmp_path):
    a = Image.open(sample("scanned_invoice.jpg")).convert("L")
    b = Image.new("L", a.size, 255)  # blank second page
    p = tmp_path / "two.tiff"
    a.save(p, save_all=True, append_images=[b])
    ex = ocr_processor.process(str(p), batch_size=1)
    pages = collect(ex)
    assert [x.page_number for x in pages] == [1, 2]
    assert "INV-2026-0042" in pages[0].text
    assert pages[1].text == "" and pages[1].ocr_confidence == 0.0


def test_deskew_finds_and_removes_tilt():
    gray = gray_sample()  # the sample is itself scanned ~1.5 degrees crooked
    base = ocr_processor.estimate_skew(gray)
    assert 0.5 < abs(base) < 2.5
    tilted = ocr_processor.rotate(gray, 4.0)
    angle = ocr_processor.estimate_skew(tilted)
    assert abs(angle - (base - 4.0)) < 1.0
    fixed = ocr_processor.rotate(tilted, angle)
    assert abs(ocr_processor.estimate_skew(fixed)) < 1.0


def test_blank_image_has_no_skew():
    assert ocr_processor.estimate_skew(np.full((500, 400), 255, np.uint8)) == 0.0


def test_preprocess_keeps_uint8_grayscale():
    out = ocr_processor.preprocess(gray_sample())
    assert out.dtype == np.uint8 and out.ndim == 2


def test_to_grayscale_handles_modes():
    for mode in ("1", "L", "P", "RGB", "RGBA", "CMYK", "I;16"):
        arr = ocr_processor.to_grayscale(Image.new(mode, (20, 10)))
        assert arr.shape == (10, 20) and arr.dtype == np.uint8


def test_transparent_png_is_composited_on_white():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))  # fully transparent
    assert ocr_processor.to_grayscale(img).min() == 255


def test_corrupted_image(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)
    with pytest.raises(CorruptedFileError):
        ocr_processor.process(str(p))


def test_missing_tesseract_gives_clear_error(monkeypatch):
    def boom(*a, **k):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    with pytest.raises(OCRUnavailableError):
        ocr_processor.ocr_array(gray_sample())


def test_tesseract_crash_with_binary_error_output_is_a_clear_error(monkeypatch):
    def boom(*a, **k):  # what pytesseract does when tesseract fails and its stderr is not valid UTF-8
        raise UnicodeDecodeError("utf-8", b"\x89PNG", 0, 1, "invalid start byte")

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    with pytest.raises(OCRError) as e:
        ocr_processor.ocr_array(gray_sample())
    assert "utf-8" not in e.value.message and "Tesseract" in e.value.message
