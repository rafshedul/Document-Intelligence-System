# Document Intelligence System

Upload invoices (PDF, scans, photos, Excel, CSV) and get structured data back: invoice number, date, supplier,
customer, subtotal, tax, total, currency and line items. Each value comes with a confidence score and the exact
text it was read from. Deterministic rules then check the result (sums, duplicates, consistency across
documents), and anything doubtful is sent to a person for review instead of being trusted.

> **Status: working prototype, not production-ready.** One shared API token, no user accounts, no tenant
> isolation, no TLS. See [Limitations](#limitations) and [docs/security.md](docs/security.md).

## Contents

[Architecture](#architecture) · [Run it](#run-it) · [Configuration](#configuration-environment-variables) ·
[Using the API](#using-the-api) · [Tests](#tests) · [Evaluation](#measuring-accuracy) ·
[Design decisions](#design-decisions) · [Limitations](#limitations) · [More docs](#more-documentation)

## Architecture

```mermaid
flowchart LR
    UI[Next.js web UI] -->|upload, poll, search| API[FastAPI]
    API -->|save file| FS[(Upload volume)]
    API -->|document row: queued| PG[(PostgreSQL + pgvector)]
    API -->|enqueue job| R[(Redis)]
    R --> W[Celery worker]
    W --> T{File type}
    T -->|digital PDF| PDF[PyMuPDF text]
    T -->|scan, photo, scanned PDF| OCR[Tesseract OCR]
    T -->|XLSX, XLS, CSV| SH[Sheet reader + column mapper]
    PDF --> TXT[Text per page]
    OCR --> TXT
    SH --> TXT
    TXT --> SEG[Split into invoices]
    SEG --> LLM[LLM extraction]
    LLM --> VER[Check every value against its source text]
    VER --> VAL[9 validation rules]
    VAL --> PG
    TXT --> EMB[Chunk and embed]
    EMB --> PG
```

In words: the API only accepts, checks and queues files. A Celery worker does the heavy work in stages, and
each stage is recorded in `processing_history` with its duration, so progress and failures are visible.

```
upload -> detect type (extension + magic bytes) -> extract text (PDF / OCR / spreadsheet)
       -> split into invoices -> LLM extraction (null unless the text says so)
       -> verify every value against its source snippet -> validate -> completed / needs_review
       -> chunk + embed (search)
```

| Part | Technology |
|---|---|
| API | FastAPI, Pydantic, SQLAlchemy 2.0, Alembic |
| Background work | Celery + Redis (retries with backoff; `inline` mode without Redis for local development) |
| Database | PostgreSQL 16 + pgvector (HNSW cosine index), full-text search (GIN) |
| Text extraction | PyMuPDF, Tesseract (with deskew / denoise / rotation detection), openpyxl, xlrd, pandas |
| Extraction | Provider-neutral LLM interface; Anthropic implemented; a rule-based `demo` stand-in for trying it without a key |
| Web UI | Next.js (TypeScript) |

## Run it

### With Docker (the web UI, API, worker, Postgres and Redis together)

```bash
cp .env.example .env
#   edit .env:  API_AUTH_TOKEN=<output of: openssl rand -hex 32>
#               LLM_API_KEY=<your key>      (or LLM_PROVIDER=demo to try it with no key)
#               POSTGRES_PASSWORD, and the same password inside DATABASE_URL
docker compose up --build
```

Then open <http://localhost:3000> (the web UI asks for the token) or <http://localhost:8000/docs> (Swagger UI).
The backend container applies the database migrations itself at start-up.

It is one command after the one-time `.env` step: the file holds secrets, so it cannot be shipped ready-made.
The server refuses to start without `API_AUTH_TOKEN` (16+ characters) unless `ENVIRONMENT=development`.

> Not verified: `docker` is not installed on the machine where this repository was written, so the Docker setup was never run. The
> Dockerfiles and `docker-compose.yml` are written for it, but treat the first `docker compose up` as untested
> and report what breaks. Everything else in this README was run.

### Without Docker (development)

Needs Python 3.12+, Node 20+, PostgreSQL with the pgvector extension, Tesseract (add `ben` for Bengali), and
Redis only if you use the Celery mode.

```bash
cp .env.example .env                       # then edit it; never commit .env
python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt
(cd frontend && npm install && cp .env.example .env.local)
psql -d YOURDB -f infra/postgres/01-extensions.sql      # CREATE EXTENSION vector
(cd backend && .venv/bin/alembic upgrade head)

(cd backend && .venv/bin/uvicorn app.main:app --port 8000)    # API
(cd frontend && npm run dev)                                  # UI on :3000
```

Quick try with no API key and no Redis: in `backend/.env` set `ENVIRONMENT=development`, `LLM_PROVIDER=demo`,
`TASK_BACKEND=inline`, then upload files from `sample_data/`. The `demo` extractor is a set of regular
expressions that reads simple "Label: value" invoices, **not an AI model**; use `LLM_PROVIDER=anthropic` and
`LLM_API_KEY` for real extraction. More in [docs/running_locally.md](docs/running_locally.md).

## Configuration (environment variables)

Real environment variables win over `backend/.env`, which wins over the project's `.env`. The full list with
defaults is in [backend/app/core/config.py](backend/app/core/config.py); the ones you are likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `ENVIRONMENT` | `production` | `development` allows running with no token. Anything else requires `API_AUTH_TOKEN` |
| `API_AUTH_TOKEN` | (empty) | Shared secret for the API and the web login. `openssl rand -hex 32` |
| `CORS_ORIGINS` | `http://localhost:3000` | Browser origins allowed to call the API |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Where the **browser** reaches the API (fixed when the frontend image is built) |
| `DATABASE_URL` | required | `postgresql://user:pass@host:5432/db` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `docintel` / `change-me` / `docintel` | Used by the Postgres container; keep in step with `DATABASE_URL` |
| `REDIS_URL` | required | Celery broker |
| `TASK_BACKEND` | `celery` | `celery` (worker + Redis) or `inline` (inside the API process; development only, no retries) |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `demo` (rule-based stand-in) |
| `LLM_API_KEY` | (empty) | Provider key. Never logged or returned by the API |
| `LLM_MODEL` / `LLM_EFFORT` | `claude-opus-5-5` / `high` | Model and reasoning effort |
| `LLM_CONCURRENCY` | `4` | Parallel LLM calls per document |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `120` | Per call |
| `UPLOAD_DIR` | `/data/uploads` | Where original files are kept |
| `MAX_FILE_SIZE_MB` | `200` | Largest accepted upload (enforced while streaming) |
| `MAX_FILES_PER_UPLOAD` / `UPLOAD_RATE_LIMIT_PER_MINUTE` | `20` / `60` | Per request / per client IP |
| `OCR_LANGUAGES` | `eng` (`eng+ben` in `.env.example`) | Tesseract languages; the Docker image has both installed |
| `OCR_DPI`, `OCR_TIMEOUT_SECONDS` | `300`, `120` | Rendering resolution and per-page time limit |
| `EMBEDDING_PROVIDER` / `EMBEDDING_DIMENSION` | `hash` / `1536` | `hash` is a demo embedder (not semantic); `none` skips the stage. The dimension is fixed by the migration |
| `TASK_MAX_RETRIES`, `TASK_RETRY_BACKOFF_SECONDS` | `3`, `5` | Temporary failures retry after 5, 10, 20 s |
| `CELERY_VISIBILITY_TIMEOUT_SECONDS` | `14400` | How long before a dead worker's job is given to another worker. Keep above your slowest job |
| `VALIDATION_TOLERANCE` | `0.02` | Rounding tolerance for sums and products |
| `VALIDATION_MIN_CONFIDENCE` | `0.7` | Below this a value gets a `LOW_CONFIDENCE` warning |
| `DOCS_ENABLED` | `true` | Swagger UI at `/docs` (endpoints still need the token) |
| `EVAL_DATABASE_URL` | none | Only for `evaluation/run_evaluation.py`: an empty throw-away database |

## Using the API

Every `/api` route needs `Authorization: Bearer <API_AUTH_TOKEN>` (except `/health`). Full reference:
[docs/api.md](docs/api.md), or `/docs` on the running server.

```bash
TOKEN=your-token; API=http://localhost:8000

# upload one or more files (repeat -F files=@...); answers 202 at once, processing runs in the background
curl -H "Authorization: Bearer $TOKEN" -F files=@sample_data/digital_invoice.pdf $API/api/documents/upload
# {"accepted":1,"rejected":0,"files":[{"filename":"digital_invoice.pdf","status":"queued","id":"<ID>","error":null}]}

# where is it? status: queued / processing / completed / needs_review / failed, plus stage and progress 0-100
curl -H "Authorization: Bearer $TOKEN" $API/api/documents/<ID>/status

# the structured result: fields (value, confidence, source_snippet), line items, validation findings, stage history
curl -H "Authorization: Bearer $TOKEN" $API/api/documents/<ID>/result

# list with filters, correct a value by hand, run it again, delete
curl -H "Authorization: Bearer $TOKEN" "$API/api/documents?status=needs_review&page=1&page_size=20"
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"invoice_index":0,"fields":{"total":"757.05"},"corrected_by":"alice"}' $API/api/documents/<ID>/result
curl -X POST   -H "Authorization: Bearer $TOKEN" $API/api/documents/<ID>/reprocess
curl -X DELETE -H "Authorization: Bearer $TOKEN" $API/api/documents/<ID>

# search: mode=keyword | semantic | hybrid (default), with filters such as supplier, amount_min, amount_max, status
curl -H "Authorization: Bearer $TOKEN" "$API/api/search?q=hosting&supplier=acme"
```

A rejected upload explains why in the same response, for example `409 duplicate_file` (same SHA-256 as an earlier
file), `413 file_too_large`, `415 unsupported_format` or `extension_mismatch`. Every error has the shape
`{"error": {"code", "message", "details", "request_id"}}`; stack traces never reach the client.

## Tests

```bash
# a throw-away database: the suite deletes every row of its `documents` table
TEST_DATABASE_URL=postgresql+psycopg://USER@HOST:PORT/TESTDB \
  backend/.venv/bin/python -m pytest tests -q --cov=app --cov-branch --cov-report=term-missing

cd frontend && npm run typecheck && npm run lint && npm test      # frontend unit tests
```

The LLM is always a mock: no test calls a real API. Tests that need a database skip themselves without
`TEST_DATABASE_URL`; OCR tests skip without Tesseract. The test database needs the pgvector extension and
`alembic upgrade head`. Browser scripts are in `tests/e2e/`. Results, what the tests found, and known limits:
[docs/test_results.md](docs/test_results.md). A manual acceptance list: [docs/manual_test_checklist.md](docs/manual_test_checklist.md).

## Measuring accuracy

`evaluation/` measures the system on documents **you** label: field accuracy, whole-invoice accuracy, line item
precision/recall, processing time per file type, success and failure rate, null rate, hallucination rate and
how well validation catches errors. Every number in the report is computed from your `ground_truth.json` and
from what the pipeline stored in a real run.

```bash
EVAL_DATABASE_URL=postgresql://USER@HOST:PORT/EVALDB  python evaluation/run_evaluation.py --reset
# -> evaluation/report.md and evaluation/results.json
```

The format and rules for the answers are in [evaluation/labeled_dataset/README.md](evaluation/labeled_dataset/README.md).
**The report in the repository comes from a synthetic 11-document sample run with the `demo` extractor.** It
checks that the measuring works, not how accurate the system is on real invoices or with a real LLM. Add your
20-30 documents and run it with a real model to get numbers that mean something.

## Design decisions

| Decision | Why |
|---|---|
| **The model may only say what the text says; everything else is null.** The prompt demands a verbatim `source_snippet` for every value, and code then checks that the snippet is in the document text and the value is in the snippet | A wrong invoice total is worse than an empty field. A prompt can be ignored, code cannot, so the rule is enforced after the model answers. A test feeds a model that invents a whole invoice and checks that every invented value is dropped |
| **Ambiguous dates (03/04/2026) become null** | Day/month order cannot be known from the text. Found by a test: it was first enforced only in the prompt |
| **Validation is plain code, not the LLM** (9 rules, `Decimal` maths, tolerance 0.02) | Arithmetic must be exact, repeatable and explainable. The LLM extracts; rules judge |
| **Doubtful results go to `needs_review`, they are not rejected or silently accepted** | A person decides. Corrections are kept in a log with old value, new value, who and when |
| **A background worker (Celery + Redis), not work inside the request** | OCR of a 1000-page PDF takes minutes. The request returns at once; progress is stored per stage and per page batch |
| **PostgreSQL with pgvector for everything** | One database for metadata, results, full-text and vectors: one backup, one transaction boundary, no sync between a search engine and the data. Money is `NUMERIC(14,2)`, never float |
| **Idempotent jobs** (pages, results of one attempt, line items, findings and embeddings are replaced, not appended) | A job can run twice (a retry, or Redis handing a dead worker's job to another). Tests run every stage 2-3 times and check for duplicates |
| **Text is extracted in page batches and saved as it goes** | Bounded memory and visible progress. A crash keeps the pages done so far |
| **OCR: render at 300 DPI, deskew, denoise, and try rotation detection when confidence is low** | Found by tests: upside-down or sideways pages were read as nonsense. Pages OCR cannot read give low confidence, not invented text |
| **Spreadsheets with recognisable headers are mapped by code; only unrecognised sheets go to the LLM** | Faster, cheaper and exact for the common case. Encoding and delimiter are detected |
| **Provider-neutral LLM interface** | Switching provider means one new class. Retries with backoff and a shared rate-limit pause live outside the provider |
| **Shared bearer token, fail closed** | The simplest protection that is not nothing. Without a token the server refuses to start outside development. Accounts are a separate, larger piece of work |
| **File type by content, not by name; random stored names; size checked while streaming** | A renamed file or a `../../x` filename must not get through |
| **Tests found bugs, and they are written up** | See [docs/test_results.md](docs/test_results.md): for example an upload that hung 20 s when Redis was down |

## Limitations

- **Not production-ready.** One shared token: everyone who has it sees every document. No users, roles or tenants,
  no TLS, no brute-force protection on the token, no antivirus scan. The list is in [docs/security.md](docs/security.md).
- **Accuracy on real documents is not yet measured.** The only evaluation run so far used a regex stand-in on
  clean synthetic invoices. Nothing here supports a claim like "95% accurate".
- **A real value under the wrong label is accepted.** If the text says `Order number: ORD-555` and the model
  reports it as the invoice number, both value and snippet really are in the text, so no check can object.
- **OCR:** tilt above about 15 degrees is not corrected (the page reads with low confidence); low OCR confidence
  does not by itself send a document to review; only English and Bengali data are set up, and Bengali was not run
  here.
- **Search:** the built-in `hash` embedder matches shared words, not meaning, so "semantic" search behaves like
  fuzzy keyword search until a real embedding model is plugged in.
- **Several invoices on one page** are split by heuristics; unusual layouts may be merged or split wrongly.
- **Dead-worker recovery** takes up to `CELERY_VISIBILITY_TIMEOUT_SECONDS` (4 h by default); *Reprocess* fixes
  a stuck document sooner. Two workers can run one job at once if it outlives that timeout; data is protected by
  replace-style writes and unique keys, but one of them may fail.
- Upload rate limiting is per API process. Invoices are sent to the LLM provider: check its retention terms.
- The Docker setup is untested (see above).

## More documentation

[architecture](docs/architecture.md) · [processing pipeline](docs/processing_pipeline.md) ·
[extraction](docs/extraction.md) · [validation rules](docs/validation.md) · [search](docs/search.md) ·
[database schema](docs/database_schema.md) · [API](docs/api.md) · [frontend](docs/frontend.md) ·
[security](docs/security.md) · [test results](docs/test_results.md) · [AI disclosure](docs/ai_disclosure.md)

`backend/app/` API, models, services, workers · `backend/alembic/` migrations · `frontend/` UI ·
`sample_data/` small synthetic files · `evaluation/` accuracy measurement · `tests/` · `docs/`
