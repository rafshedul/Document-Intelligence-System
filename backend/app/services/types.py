from dataclasses import dataclass, field
from typing import Iterator

from app.models.enums import ExtractionMethod


@dataclass
class PageResult:
    """Text of one document_pages row (a PDF/image page, or a chunk of sheet rows)."""

    page_number: int  # 1-based
    text: str
    method: ExtractionMethod
    ocr_confidence: float | None = None  # 0-100, only for OCR
    error: str | None = None  # set when this page failed; text is then ""


@dataclass
class Extraction:
    """Lazy result of a processor.

    `batches` yields lists of pages so callers can persist and free memory as
    they go. `info` is filled while iterating (page counts, skipped sheets...).
    """

    batches: Iterator[list[PageResult]]
    info: dict = field(default_factory=dict)

    def close(self) -> None:
        close = getattr(self.batches, "close", None)
        if close:
            close()
