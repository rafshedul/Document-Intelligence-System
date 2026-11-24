import enum


class DocumentStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    needs_review = "needs_review"


class ExtractionMethod(str, enum.Enum):
    digital = "digital"
    ocr = "ocr"
    spreadsheet = "spreadsheet"


class Severity(str, enum.Enum):
    error = "error"
    warning = "warning"
