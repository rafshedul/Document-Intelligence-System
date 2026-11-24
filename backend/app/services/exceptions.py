"""Errors raised by the file-processing pipeline.

Every expected failure has its own class and a stable `code`, so callers can
branch on the type and the UI can show `message` to a user as is.
"""


class DocumentProcessingError(Exception):
    code = "processing_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class UnsupportedFormatError(DocumentProcessingError):
    code = "unsupported_format"


class ExtensionMismatchError(DocumentProcessingError):
    """File extension says one thing, the file's bytes say another."""

    code = "extension_mismatch"


class FileTooLargeError(DocumentProcessingError):
    code = "file_too_large"


class EmptyFileError(DocumentProcessingError):
    code = "empty_file"


class CorruptedFileError(DocumentProcessingError):
    code = "corrupted_file"


class PasswordProtectedError(DocumentProcessingError):
    code = "password_protected"


class OCRError(DocumentProcessingError):
    code = "ocr_failed"


class OCRUnavailableError(OCRError):
    """Tesseract (or a requested language pack) is not installed."""

    code = "ocr_unavailable"
