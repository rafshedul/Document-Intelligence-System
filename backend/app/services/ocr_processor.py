"""OCR with Tesseract: preprocessing, per-page confidence, multi-page TIFF."""
import io
import math
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.models.enums import ExtractionMethod
from app.services.exceptions import (
    CorruptedFileError,
    FileTooLargeError,
    OCRError,
    OCRUnavailableError,
)
from app.services.types import Extraction, PageResult


@dataclass
class OcrResult:
    text: str
    confidence: float  # mean word confidence, 0-100; 0.0 when nothing was read


# ---- preprocessing ---------------------------------------------------------

def to_grayscale(img: Image.Image) -> np.ndarray:
    """Any PIL image (RGBA, palette, CMYK, 16-bit...) -> uint8 grayscale array."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    if img.mode.startswith("I;16") or img.mode in ("I", "F"):
        arr = np.asarray(img, dtype=np.float64)
        lo, hi = arr.min(), arr.max()
        return ((arr - lo) / max(hi - lo, 1) * 255).astype(np.uint8)
    return np.asarray(img.convert("L"))


def estimate_skew(gray: np.ndarray, max_angle: float = 10.0) -> float:
    """Skew in degrees, found by maximising row-profile variance of the text mask.

    Returns the rotation (cv2 convention) that straightens the page, or 0.0
    when there is no clear improvement (blank pages, photos).
    """
    h, w = gray.shape
    scale = min(1.0, 1000 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if not bw.any():
        return 0.0
    sh, sw = bw.shape

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D((sw / 2, sh / 2), angle, 1.0)
        rotated = cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST)
        return float(np.var(rotated.sum(axis=1, dtype=np.float64)))

    best = max(np.arange(-max_angle, max_angle + 0.01, 1.0), key=score)
    best = max(np.arange(best - 1.0, best + 1.01, 0.1), key=score)
    if score(best) < score(0.0) * 1.05:
        return 0.0
    return float(best)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def preprocess(gray: np.ndarray) -> np.ndarray:
    """Upscale tiny images, denoise, deskew, boost contrast."""
    if max(gray.shape) < 1200:  # tiny scans/screenshots: Tesseract wants ~30px text height
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.fastNlMeansDenoising(gray, None, h=10, templateWindowSize=7, searchWindowSize=21)
    angle = estimate_skew(gray)
    if abs(angle) >= 0.3:
        gray = rotate(gray, angle)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


# ---- recognition -----------------------------------------------------------

def ocr_array(gray: np.ndarray, do_preprocess: bool = True) -> OcrResult:
    if gray.size > settings.ocr_max_pixels:
        raise FileTooLargeError(f"Image is too large to OCR ({gray.shape[1]}x{gray.shape[0]} px).")
    img = preprocess(gray) if do_preprocess else gray
    try:
        data = pytesseract.image_to_data(
            Image.fromarray(img),
            lang=settings.ocr_languages,
            config="--psm 3",
            output_type=pytesseract.Output.DICT,
            timeout=settings.ocr_timeout_seconds,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRUnavailableError("Tesseract OCR is not installed on this server.") from exc
    except pytesseract.TesseractError as exc:
        msg = str(exc)
        if "load" in msg.lower() and "lang" in msg.lower():
            raise OCRUnavailableError(
                f"Tesseract language data for '{settings.ocr_languages}' is not installed."
            ) from exc
        raise OCRError(f"OCR failed: {msg[:200]}") from exc
    except (UnicodeDecodeError, OSError) as exc:  # Tesseract crashed and its error output was not text
        raise OCRError("OCR could not run on this image (Tesseract failed).") from exc
    except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
        raise OCRError(f"OCR timed out after {settings.ocr_timeout_seconds}s.") from exc

    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confs.append(conf)
    text = "\n".join(" ".join(words) for words in lines.values())
    return OcrResult(text=text, confidence=round(sum(confs) / len(confs), 2) if confs else 0.0)


# ---- image files (JPG / PNG / TIFF) ---------------------------------------

def process(path: str, batch_size: int | None = None) -> Extraction:
    batch_size = batch_size or settings.image_batch_size
    try:
        with Image.open(path) as img:
            n_frames = getattr(img, "n_frames", 1)
            img.load() if n_frames == 1 else img.seek(0)
    except Image.DecompressionBombError as exc:
        raise FileTooLargeError("The image has too many pixels to process safely.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise CorruptedFileError("The image file is damaged or cannot be decoded.") from exc

    info = {"page_count": n_frames, "ocr_pages": 0, "failed_pages": []}

    def gen():
        with Image.open(path) as img:
            for start in range(0, n_frames, batch_size):
                batch = []
                for i in range(start, min(n_frames, start + batch_size)):
                    batch.append(_ocr_frame(img, i, info))
                yield batch

    return Extraction(gen(), info)


def _ocr_frame(img: Image.Image, index: int, info: dict) -> PageResult:
    number = index + 1
    try:
        img.seek(index)
        frame = ImageOps.exif_transpose(img.copy())
        result = ocr_array(to_grayscale(frame))
        info["ocr_pages"] += 1
        return PageResult(number, result.text, ExtractionMethod.ocr, result.confidence)
    except OCRUnavailableError:
        raise  # system-level problem: every page would fail
    except Exception as exc:
        info["failed_pages"].append({"page": number, "error": str(exc)[:300]})
        return PageResult(number, "", ExtractionMethod.ocr, None, error=str(exc)[:300])
