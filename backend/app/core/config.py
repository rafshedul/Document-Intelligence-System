from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Real environment variables win; otherwise read backend/.env, then the project's .env.
    # (In Docker the variables come from compose, and no .env file exists in the image.)
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    # "development" allows running without a token; anything else requires API_AUTH_TOKEN.
    environment: str = "production"
    api_auth_token: str = ""  # shared secret for the API; generate with: openssl rand -hex 32
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    task_backend: str = "celery"  # "celery" (Redis worker) or "inline" (FastAPI background task, dev only)
    docs_enabled: bool = True  # Swagger UI at /docs; the endpoints themselves still need the token
    max_files_per_upload: int = 20
    upload_rate_limit_per_minute: int = 60  # files per client IP per minute
    task_max_retries: int = 3
    # Redis hands a dead worker's job to another worker only after this long. Keep it above the slowest job.
    celery_visibility_timeout_seconds: int = 4 * 3600
    task_retry_backoff_seconds: float = 5.0  # waits 5s, 10s, 20s (doubling)
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
    # Embeddings: "hash" = built-in lexical hashing (no model, for demos/tests), "none" = skip the stage
    embedding_provider: str = "hash"
    embedding_chunk_tokens: int = 500  # target chunk size
    embedding_chunk_overlap_ratio: float = 0.10  # each chunk repeats the last 10% of the previous one
    chars_per_token: float = 4.0  # rough estimate for Latin text (tokens are not counted exactly)
    embedding_batch_size: int = 64

    # Search
    search_candidates: int = 100  # documents taken from each ranking before fusion
    search_max_results: int = 500  # results kept per query (total is capped here)
    search_semantic_min_score: float = 0.05  # cosine similarity below this is not a match
    search_rrf_k: int = 60  # Reciprocal Rank Fusion constant

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
