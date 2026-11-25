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


# ---- image quality, rotation, multi-page TIFF -------------------------------------------------------------

import cv2  # noqa: E402

INVOICE_NO = "INV-2026-0042"


def blur(g, sigma):
    return cv2.GaussianBlur(g, (0, 0), sigma)


def noisy(g, sigma, seed=0):
    return np.clip(g.astype(float) + np.random.default_rng(seed).normal(0, sigma, g.shape), 0, 255).astype(np.uint8)


@needs_tesseract
def test_clean_image_reads_well():
    r = ocr_processor.ocr_array(gray_sample())
    assert INVOICE_NO in r.text and r.confidence > 90


@needs_tesseract
def test_confidence_drops_as_image_quality_drops():
    g = gray_sample()
    clean = ocr_processor.ocr_array(g).confidence
    soft = ocr_processor.ocr_array(blur(g, 3)).confidence
    ruined = ocr_processor.ocr_array(blur(g, 6)).confidence
    assert clean > soft > ruined and clean - soft > 5  # worse picture, lower confidence
    assert ruined < 30


@needs_tesseract
@pytest.mark.parametrize("name,make", [
    ("heavy noise", lambda g: noisy(g, 70)),
    ("random static", lambda g: np.random.default_rng(1).integers(0, 256, g.shape).astype(np.uint8)),
    ("blank page", lambda g: np.full(g.shape, 255, np.uint8)),
    ("black page", lambda g: np.zeros(g.shape, np.uint8)),
])
def test_unreadable_images_give_low_confidence_and_no_invented_text(name, make):
    r = ocr_processor.ocr_array(make(gray_sample()))
    assert r.confidence < 30, name
    assert INVOICE_NO not in r.text and len(r.text) < 40  # nothing made up


@needs_tesseract
def test_low_resolution_and_low_contrast_still_read():
    g = gray_sample()
    small = cv2.resize(g, None, fx=0.3, fy=0.3, interpolation=cv2.INTER_AREA)
    assert INVOICE_NO in ocr_processor.ocr_array(small).text
    faded = (g * 0.2 + 180).astype(np.uint8)
    assert INVOICE_NO in ocr_processor.ocr_array(faded).text


@needs_tesseract
@pytest.mark.parametrize("angle", [-10, -5, 5, 10])
def test_tilted_scans_are_straightened(angle):
    r = ocr_processor.ocr_array(ocr_processor.rotate(gray_sample(), angle))
    assert INVOICE_NO in r.text and r.confidence > 85


@needs_tesseract
@pytest.mark.parametrize("name,flag", [("90 clockwise", cv2.ROTATE_90_CLOCKWISE), ("180", cv2.ROTATE_180),
                                       ("90 counter-clockwise", cv2.ROTATE_90_COUNTERCLOCKWISE)])
def test_sideways_and_upside_down_pages_are_turned_upright(name, flag):
    r = ocr_processor.ocr_array(cv2.rotate(gray_sample(), flag))
    assert INVOICE_NO in r.text and r.confidence > 85, name


@needs_tesseract
def test_without_auto_rotation_an_upside_down_page_is_unreadable():
    upside_down = cv2.rotate(gray_sample(), cv2.ROTATE_180)
    assert ocr_processor.ocr_array(upside_down, auto_rotate=False).confidence < 60  # proves the feature does the work


@needs_tesseract
def test_a_page_tilted_far_beyond_the_limit_is_flagged_by_low_confidence_not_guessed():
    r = ocr_processor.ocr_array(ocr_processor.rotate(gray_sample(), 35))
    assert r.confidence < 60 and INVOICE_NO not in r.text  # known limit: tilt above ~15 degrees


@needs_tesseract
def test_a_good_page_is_not_sent_through_the_rotation_check(monkeypatch):
    called = []
    monkeypatch.setattr(ocr_processor, "detect_rotation", lambda g: called.append(1) or 0)
    ocr_processor.ocr_array(gray_sample())
    assert called == []  # no extra Tesseract run for pages that already read well


def test_failure_of_the_orientation_check_does_not_lose_the_first_read(monkeypatch):
    def boom(*a, **k):
        raise pytesseract.TesseractError(1, "Too few characters. Skipping this page")

    monkeypatch.setattr(pytesseract, "image_to_osd", boom)
    assert ocr_processor.detect_rotation(gray_sample()) == 0


@needs_tesseract
def test_multi_page_tiff_with_mixed_pages(tmp_path):
    g = Image.open(sample("scanned_invoice.jpg")).convert("L")
    upside = g.rotate(180)
    blank = Image.new("L", g.size, 255)
    p = tmp_path / "three.tif"
    g.save(p, save_all=True, append_images=[upside, blank])
    ex = ocr_processor.process(str(p), batch_size=2)
    pages = collect(ex)
    assert [x.page_number for x in pages] == [1, 2, 3] and ex.info["page_count"] == 3 and ex.info["failed_pages"] == []
    assert INVOICE_NO in pages[0].text and INVOICE_NO in pages[1].text  # page 2 was upside down
    assert pages[2].text == "" and pages[2].ocr_confidence == 0.0
    assert pages[0].ocr_confidence > 90 and pages[1].ocr_confidence > 85


@needs_tesseract
def test_a_bad_frame_does_not_stop_the_other_pages(tmp_path, monkeypatch):
    g = Image.open(sample("scanned_invoice.jpg")).convert("L")
    p = tmp_path / "two.tif"
    g.save(p, save_all=True, append_images=[g])
    real = ocr_processor.ocr_array
    calls = []

    def flaky(arr, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OCRError("OCR timed out")
        return real(arr, *a, **k)

    monkeypatch.setattr(ocr_processor, "ocr_array", flaky)
    ex = ocr_processor.process(str(p))
    pages = collect(ex)
    assert pages[0].error and pages[0].text == "" and INVOICE_NO in pages[1].text
    assert ex.info["failed_pages"][0]["page"] == 1


def test_tesseract_failures_are_told_apart(monkeypatch):
    def raises(exc):
        def f(*a, **k):
            raise exc
        return f

    g = gray_sample()
    monkeypatch.setattr(pytesseract, "image_to_data", raises(pytesseract.TesseractError(1, "Failed loading language 'ben'")))
    with pytest.raises(OCRUnavailableError) as e:
        ocr_processor.ocr_array(g, auto_rotate=False)
    assert "language" in e.value.message.lower()
    monkeypatch.setattr(pytesseract, "image_to_data", raises(pytesseract.TesseractError(1, "Segmentation fault")))
    with pytest.raises(OCRError) as e:
        ocr_processor.ocr_array(g, auto_rotate=False)
    assert not isinstance(e.value, OCRUnavailableError)
    monkeypatch.setattr(pytesseract, "image_to_data", raises(RuntimeError("Tesseract process timeout")))
    with pytest.raises(OCRError) as e:
        ocr_processor.ocr_array(g, auto_rotate=False)
    assert "timed out" in e.value.message


def test_an_image_with_too_many_pixels_is_refused_not_attempted(monkeypatch):
    from app.core.config import settings
    from app.services.exceptions import FileTooLargeError

    monkeypatch.setattr(settings, "ocr_max_pixels", 1000)
    with pytest.raises(FileTooLargeError):
        ocr_processor.ocr_array(gray_sample())
