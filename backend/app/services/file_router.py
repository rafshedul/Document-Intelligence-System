"""Pick the right processor from the file's extension AND its magic bytes."""
import os
import zipfile
from dataclasses import dataclass
from enum import Enum

from app.core.config import settings
from app.services.exceptions import (
    CorruptedFileError,
    EmptyFileError,
    ExtensionMismatchError,
    FileTooLargeError,
    PasswordProtectedError,
    UnsupportedFormatError,
)


class FileKind(str, Enum):
    pdf = "pdf"
    image = "image"
    xlsx = "xlsx"
    xls = "xls"
    csv = "csv"


# extension -> concrete format. Formats are compared exactly, so a PNG named
# .jpg is reported instead of silently accepted.
EXTENSION_FORMATS = {
    ".pdf": "pdf",
    ".jpg": "jpeg", ".jpeg": "jpeg",
    ".png": "png",
    ".tif": "tiff", ".tiff": "tiff",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".csv": "csv",
}
FORMAT_KINDS = {
    "pdf": FileKind.pdf,
    "jpeg": FileKind.image, "png": FileKind.image, "tiff": FileKind.image,
    "xlsx": FileKind.xlsx, "xls": FileKind.xls, "csv": FileKind.csv,
}

_PNG = b"\x89PNG\r\n\x1a\n"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")


@dataclass(frozen=True)
class Detection:
    format: str  # pdf | jpeg | png | tiff | xlsx | xls | csv
    kind: FileKind
    extension: str


def detect(path: str, original_filename: str | None = None) -> Detection:
    """Validate the file and return what it is. Raises a DocumentProcessingError."""
    name = original_filename or path
    ext = os.path.splitext(name)[1].lower()

    size = os.path.getsize(path)
    if size == 0:
        raise EmptyFileError("The file is empty (0 bytes).")
    limit = settings.max_file_size_mb * 1024 * 1024
    if size > limit:
        raise FileTooLargeError(
            f"The file is {size / 1024 / 1024:.1f} MB; the limit is {settings.max_file_size_mb} MB."
        )
    if ext not in EXTENSION_FORMATS:
        raise UnsupportedFormatError(
            f"Files of type '{ext or 'no extension'}' are not supported. "
            "Supported: PDF, JPG, PNG, TIFF, XLSX, XLS, CSV."
        )

    fmt = _sniff(path, ext)
    declared = EXTENSION_FORMATS[ext]
    if fmt is None:
        raise CorruptedFileError(
            f"The file is named '{ext}' but its content is not a recognizable {declared.upper()} file."
        )
    if fmt != declared:
        raise ExtensionMismatchError(
            f"The file extension '{ext}' says {declared.upper()}, but the content is {fmt.upper()}. "
            "Rename the file or upload the correct one.",
            declared=declared, detected=fmt,
        )
    return Detection(format=fmt, kind=FORMAT_KINDS[fmt], extension=ext)


def get_processor(kind: FileKind):
    """Return the processor module (each has `process(path) -> Extraction`)."""
    from app.services import ocr_processor, pdf_processor, spreadsheet_processor

    return {
        FileKind.pdf: pdf_processor,
        FileKind.image: ocr_processor,
        FileKind.xlsx: spreadsheet_processor,
        FileKind.xls: spreadsheet_processor,
        FileKind.csv: spreadsheet_processor,
    }[kind]


def _sniff(path: str, ext: str) -> str | None:
    with open(path, "rb") as f:
        head = f.read(4096)

    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(_PNG):
        return "png"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip(path)
    if head.startswith(_OLE):
        # Password-protected .xlsx is an OLE container with an EncryptedPackage stream.
        if _has_encrypted_package(path):
            raise PasswordProtectedError("The spreadsheet is password-protected. Remove the password and upload again.")
        return "xls"
    if _looks_like_text(head):
        return "csv"
    return None


def _sniff_zip(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError("The file is a damaged ZIP/Office archive.") from exc
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise UnsupportedFormatError(
        "This is a ZIP/Office file but not an Excel (.xlsx) workbook, which is not supported."
    )


def _has_encrypted_package(path: str, max_bytes: int = 64 * 1024 * 1024) -> bool:
    with open(path, "rb") as f:
        tail = b""
        read = 0
        while chunk := f.read(1024 * 1024):
            if _ENCRYPTED_PACKAGE in tail + chunk:
                return True
            tail = chunk[-len(_ENCRYPTED_PACKAGE):]
            read += len(chunk)
            if read > max_bytes:
                break
    return False


def _looks_like_text(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):  # BOMs: UTF-16 / UTF-8
        return True
    if b"\x00" in head:
        return False
    control = sum(1 for b in head if b < 9 or 13 < b < 32)
    return control / max(len(head), 1) < 0.01
