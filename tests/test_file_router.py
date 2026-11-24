import shutil

import pytest
from conftest import sample

from app.core.config import settings
from app.services import file_router
from app.services.exceptions import (
    CorruptedFileError,
    EmptyFileError,
    ExtensionMismatchError,
    FileTooLargeError,
    PasswordProtectedError,
    UnsupportedFormatError,
)
from app.services.file_router import FileKind


@pytest.mark.parametrize("name,fmt,kind", [
    ("digital_invoice.pdf", "pdf", FileKind.pdf),
    ("scanned_invoice.jpg", "jpeg", FileKind.image),
    ("multi_sheet.xlsx", "xlsx", FileKind.xlsx),
    ("invoices.csv", "csv", FileKind.csv),
])
def test_detects_samples(name, fmt, kind):
    d = file_router.detect(sample(name))
    assert (d.format, d.kind) == (fmt, kind)


def test_extension_mismatch_is_caught(tmp_path):
    p = tmp_path / "photo.png"  # really a JPEG
    shutil.copy(sample("scanned_invoice.jpg"), p)
    with pytest.raises(ExtensionMismatchError) as e:
        file_router.detect(str(p))
    assert "JPEG" in e.value.message and "PNG" in e.value.message

    p = tmp_path / "data.xlsx"  # really a PDF
    shutil.copy(sample("digital_invoice.pdf"), p)
    with pytest.raises(ExtensionMismatchError):
        file_router.detect(str(p))


def test_text_in_binary_extension_and_binary_in_csv(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_text("just some text, not a pdf\n")
    with pytest.raises(ExtensionMismatchError):
        file_router.detect(str(p))
    p = tmp_path / "a.csv"
    shutil.copy(sample("digital_invoice.pdf"), p)
    with pytest.raises(ExtensionMismatchError):
        file_router.detect(str(p))


def test_unknown_content_with_known_extension_is_corrupted(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(bytes(range(256)) * 10)
    with pytest.raises(CorruptedFileError):
        file_router.detect(str(p))


def test_unsupported_extension(tmp_path):
    p = tmp_path / "notes.docx"
    p.write_bytes(b"PK\x03\x04 whatever")
    with pytest.raises(UnsupportedFormatError):
        file_router.detect(str(p))


def test_empty_and_too_large(tmp_path, monkeypatch):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"")
    with pytest.raises(EmptyFileError):
        file_router.detect(str(p))
    monkeypatch.setattr(settings, "max_file_size_mb", 0)
    with pytest.raises(FileTooLargeError):
        file_router.detect(sample("digital_invoice.pdf"))


def test_encrypted_ooxml_detected_as_password_protected(tmp_path):
    p = tmp_path / "secret.xlsx"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 500 + "EncryptedPackage".encode("utf-16-le"))
    with pytest.raises(PasswordProtectedError):
        file_router.detect(str(p))


def test_original_filename_overrides_stored_name(tmp_path):
    """Stored files have generated names; the user's original name decides the extension."""
    p = tmp_path / "3f9a1c"  # no extension on disk
    shutil.copy(sample("digital_invoice.pdf"), p)
    assert file_router.detect(str(p), "invoice.PDF").format == "pdf"
