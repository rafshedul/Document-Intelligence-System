# Document Intelligence System

Upload invoices (PDF, scans, photos, Excel, CSV) and get structured data back: invoice number, date, supplier, customer, totals, currency and line items, each with a confidence score and the exact text it came from. Every result is checked by deterministic rules (sums, duplicates, cross-document consistency) and anything doubtful is sent to review instead of being trusted.

> Status: working prototype. It is **not production-ready**: there is one shared API token, no per-user accounts, and no tenant isolation. See [docs/security.md](docs/security.md).

## What it does

```
upload -> detect type (extension + magic bytes) -> extract text (PDF / OCR / spreadsheet)
       -> split into invoices -> LLM extraction (null unless the text says so)
       -> verify every value against its source snippet -> validate (9 rules) -> completed / needs_review
```

| Stage | Details | Docs |
|---|---|---|
| File processing | PDF (PyMuPDF, page batches), OCR (Tesseract + deskew/denoise), XLSX/XLS/CSV | [processing_pipeline.md](docs/processing_pipeline.md) |
| Extraction | Provider-neutral LLM interface, JSON-schema output, snippet verification, spreadsheet column mapping | [extraction.md](docs/extraction.md) |
| Validation | `MISSING_FIELD`, `INVALID_NUMBER`, `LINE_ITEM_MISMATCH`, `SUBTOTAL_MISMATCH`, `TOTAL_MISMATCH`, `DUPLICATE_INVOICE`, `DUPLICATE_FILE`, `LOW_CONFIDENCE`, `INVALID_DATE`, `CROSS_DOCUMENT` | [validation.md](docs/validation.md) |
| Database | PostgreSQL + pgvector, SQLAlchemy 2.0, Alembic | [database_schema.md](docs/database_schema.md) |

Stack: FastAPI, Celery + Redis, PostgreSQL/pgvector, Tesseract, Next.js.

## Run it locally

Needs Python 3.12+, Node 20+, PostgreSQL with the pgvector extension, Tesseract, and Redis (only for the Celery mode).

```bash
cp .env.example .env            # then edit it; never commit .env
python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm install)
(cd backend && .venv/bin/alembic upgrade head)

# backend (http://localhost:8000)
cd backend && .venv/bin/uvicorn app.main:app --port 8000
# frontend (http://localhost:3000)
cd frontend && npm run dev
```

Quick try without an API key or Redis: set `ENVIRONMENT=development`, `LLM_PROVIDER=demo`, `TASK_BACKEND=inline` in `backend/.env`, then upload files from `sample_data/` in the web page. The `demo` extractor is a rule-based stand-in, not an AI model. For real extraction use `LLM_PROVIDER=anthropic` and set `LLM_API_KEY`. More in [docs/running_locally.md](docs/running_locally.md).

With Docker: `docker compose up --build` (set `API_AUTH_TOKEN` in `.env` first: `openssl rand -hex 32`).

## API

Upload, list, status, result, manual correction, reprocess and delete: see [docs/api.md](docs/api.md) or open `/docs` (Swagger UI) on the running backend. Background work runs in Celery with retries and per-stage progress.

## Tests

```bash
TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/DBNAME backend/.venv/bin/python -m pytest tests -q
```
Without `TEST_DATABASE_URL`, tests that need a database are skipped. The LLM is always mocked; no test calls a real API. OCR tests are skipped if Tesseract is not installed.

## Security

The API requires `Authorization: Bearer <API_AUTH_TOKEN>` (except `/health`) and refuses to start in production without a token. Secrets live in `.env`, which is git-ignored. Read [docs/security.md](docs/security.md) before exposing this to a network.

## Layout

`backend/app/` api, models, services (processors, extraction, validation), workers · `backend/alembic/` migrations · `frontend/` Next.js UI · `sample_data/` small synthetic invoices (`scripts/generate_sample_data.py`) · `tests/` · `docs/`
