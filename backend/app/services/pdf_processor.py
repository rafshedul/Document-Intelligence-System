"""PDF text extraction with PyMuPDF; pages with little text are OCR'd.

Pages are loaded one at a time and yielded in batches, so a 1000-page PDF
never sits in memory (PyMuPDF itself memory-maps the file).
"""
import math

import numpy as np
import pymupdf

from app.core.config import settings
from app.models.enums import ExtractionMethod
from app.services import ocr_processor
from app.services.exceptions import (
    CorruptedFileError,
    EmptyFileError,
    OCRUnavailableError,
    PasswordProtectedError,
)
from app.services.types import Extraction, PageResult

pymupdf.TOOLS.mupdf_display_errors(False)


def process(path: str, batch_size: int | None = None) -> Extraction:
    batch_size = batch_size or settings.pdf_batch_size
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise CorruptedFileError("The PDF is damaged and cannot be opened.") from exc
    if doc.needs_pass:
        doc.close()
        raise PasswordProtectedError("The PDF is password-protected. Remove the password and upload again.")
    n = doc.page_count
    if n == 0:
        doc.close()
        raise EmptyFileError("The PDF has no pages.")

    info = {"page_count": n, "digital_pages": 0, "ocr_pages": 0, "failed_pages": []}

    def gen():
        try:
            for start in range(0, n, batch_size):
                batch = [_extract_page(doc, i, info) for i in range(start, min(n, start + batch_size))]
                yield batch
                pymupdf.TOOLS.store_shrink(100)  # drop MuPDF's cached page data
        finally:
            doc.close()

    return Extraction(gen(), info)


def _extract_page(doc: pymupdf.Document, index: int, info: dict) -> PageResult:
    number = index + 1
    try:
        page = doc.load_page(index)
        text = page.get_text("text")
        if len(text.strip()) >= settings.pdf_min_text_chars:
            info["digital_pages"] += 1
            return PageResult(number, text, ExtractionMethod.digital)

        # Too little text: treat as a scan.
        result = ocr_processor.ocr_array(_render_gray(page))
        if len(result.text.strip()) >= len(text.strip()):
            info["ocr_pages"] += 1
            return PageResult(number, result.text, ExtractionMethod.ocr, result.confidence)
        info["digital_pages"] += 1  # OCR found less than the embedded text; keep the latter
        return PageResult(number, text, ExtractionMethod.digital)
    except OCRUnavailableError:
        raise
    except Exception as exc:
        info["failed_pages"].append({"page": number, "error": str(exc)[:300]})
        return PageResult(number, "", ExtractionMethod.digital, None, error=str(exc)[:300])


def _render_gray(page: pymupdf.Page) -> np.ndarray:
    zoom = settings.ocr_dpi / 72
    rect = page.rect
    pixels = rect.width * rect.height * zoom * zoom
    if pixels > settings.ocr_max_pixels:  # e.g. poster-sized pages
        zoom *= math.sqrt(settings.ocr_max_pixels / pixels)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].copy()
