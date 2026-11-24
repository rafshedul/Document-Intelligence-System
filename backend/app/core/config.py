from decimal import Decimal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # "development" allows running without a token; anything else requires API_AUTH_TOKEN.
    environment: str = "production"
    api_auth_token: str = ""  # shared secret for the API; generate with: openssl rand -hex 32
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    task_backend: str = "inline"  # "celery" (Redis worker) or "inline" (FastAPI background task)
    database_url: str
    redis_url: str
    upload_dir: str = "/data/uploads"
    max_file_size_mb: int = 200
    # Must match the embedding model. Baked into the embeddings.embedding column
    # type at migration time; changing it later needs a new migration (re-embed).
    # pgvector's HNSW index supports at most 2000 dimensions for `vector`.
    embedding_dimension: int = 1536

    # File processing
    ocr_languages: str = "eng"  # e.g. "eng+ben" (Docker image has both installed)
    ocr_dpi: int = 300  # resolution used when rendering PDF pages for OCR
    ocr_timeout_seconds: int = 120  # per page
    ocr_max_pixels: int = 40_000_000  # cap on rendered/processed image size
    pdf_min_text_chars: int = 50  # pages with less text are treated as scanned
    pdf_batch_size: int = 25  # pages processed (and committed) per batch
    image_batch_size: int = 10  # TIFF frames per batch
    spreadsheet_rows_per_page: int = 1000  # rows per document_pages row

    # LLM extraction
    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "high"  # low | medium | high | xhigh | max
    llm_max_output_tokens: int = 8000
    llm_request_timeout_seconds: float = 120.0
    llm_use_refusal_fallbacks: bool = True  # server-side fallback on safety refusals
    llm_concurrency: int = 4  # parallel LLM calls
    llm_max_attempts: int = 5  # per call, for rate-limit/network/5xx errors
    llm_backoff_base_seconds: float = 1.0
    llm_backoff_max_seconds: float = 60.0
    llm_max_validation_retries: int = 2  # re-asks after invalid JSON (so up to 3 calls)
    # Validation
    validation_tolerance: Decimal = Decimal("0.02")  # rounding tolerance for sums and products
    validation_max_amount: Decimal = Decimal("100000000")  # larger money amounts are "unusually large"
    validation_min_confidence: float = 0.7
    extraction_max_segment_chars: int = 150_000  # one invoice larger than this -> needs_review

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL uses the plain libpq scheme; SQLAlchemy needs the driver."""
        url = self.database_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
