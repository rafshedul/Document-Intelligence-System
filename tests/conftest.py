import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.pop("API_AUTH_TOKEN", None)

import shutil  # noqa: E402

import pytest  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "sample_data")
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def sample(name: str) -> str:
    return os.path.join(SAMPLES, name)


def collect(extraction):
    """Flatten an Extraction into a list of PageResult."""
    try:
        return [p for batch in extraction.batches for p in batch]
    finally:
        extraction.close()
