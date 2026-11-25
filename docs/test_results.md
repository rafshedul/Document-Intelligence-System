# Test results

Run on 2026-11-25, macOS, Python 3.14, PostgreSQL 18 + pgvector 0.8.7, Tesseract 5.5.4 (English + OSD), Redis 8.

**500 tests: 500 passed, 0 failed, 0 skipped.** Coverage **97.2%** (2,584 of 2,633 statements; 683 of 728 branches). Run time about 12 s, 49 s with coverage.

```bash
TEST_DATABASE_URL=postgresql+psycopg://USER@HOST/DB backend/.venv/bin/python -m pytest tests -q \
    --cov=app --cov-branch --cov-report=term-missing
```

Tests that need a database skip themselves without `TEST_DATABASE_URL`; OCR tests skip without Tesseract. The suite deletes every row in `documents` of the database it is pointed at: use a throw-away database. The LLM is always a mock; nothing calls a real API.

## What each requirement is covered by

| # | Requirement | Tests | Where |
|---|---|---|---|
| 1 | Document processing: digital, multi-page, scanned, empty, password-protected PDF | digital text; 7-, 60- and 120-page batching; scanned and mixed scanned/digital; blank page; zero-page PDF; 0-byte file; encrypted PDF; one bad page among good ones; poster-size page; upside-down scanned page; Tesseract missing | `test_pdf_processor.py` (18), `test_pipeline.py` (10) |
| 2 | OCR: clean, poor-quality, rotated images, multi-page TIFF; low confidence on bad images | clean image reads at 94.8%; blur sigma 3 reads at 78%, sigma 6 at 0%; noise, static, blank and black pages: confidence under 30 and no invented text; low resolution and low contrast; tilts of -10..10 degrees; 90/180/270 rotations; tilt of 35 degrees is flagged, not guessed; 3-page TIFF (normal, upside down, blank); one bad frame does not stop the rest; Tesseract crashes and timeouts | `test_ocr_processor.py` (32) |
| 3 | Spreadsheets: several sheets, empty sheet, merged cells, CSV encodings and delimiters, old XLS | multi-sheet XLSX; empty sheets skipped; horizontal and vertical merges (XLSX and XLS); XLS with dates; 10 encodings (UTF-8/16, BOM, Latin-1, cp1252, cp1251, Shift-JIS, Bengali, Japanese); `, ; tab |` delimiters, quotes, ragged rows; 5,000-row CSV in pages; truncated and internally broken workbooks | `test_spreadsheet_processor.py` (32), `test_spreadsheet_mapper.py` (7), `test_file_router.py` (22) |
| 4 | AI extraction with a mock LLM: valid JSON, invalid JSON, missing fields, nulls, **proof that nothing is invented** | valid output typed and saved; invalid JSON retried twice with the error fed back, then `needs_review`; refusal; oversized input; a hostile mock that invents a complete invoice (all fields null, each lie logged); wrong amounts under real snippets; value not in its snippet; ambiguous dates; end to end: an invented invoice leaves an all-null result and a document in `needs_review` | `test_extraction_segment.py` (15), `test_no_fabrication.py` (16), `test_extraction_pipeline.py` (12), `test_field_verification.py` (50), `test_llm_client.py` (13) |
| 5 | Validation: pass and fail for every rule | all 9 rules (10 codes), each with good and bad data, boundaries (tolerance exactly 0.02), Decimal not float, status outcome, resolved findings kept, latest version only | `test_validation_rules.py` (35), `test_validation_service.py` (24) |
| 6 | API: every endpoint, success and failure; wrong file type, big file, missing id, wrong auth | 7 endpoints plus search and detail; mislabelled, unsupported, empty, duplicate and oversized files (including the exact 1 MB boundary); unknown ids; malformed ids; all 10 endpoints refuse a missing, wrong or malformed token, and an unauthorised upload stores nothing; rate limit; uniform error shape; no stack traces; OpenAPI | `test_api.py` (51), `test_security.py` (26), `test_search.py` (52), `test_core_utils.py` (17) |
| 7 | Failures: corrupted file, broken ZIP-like Excel, LLM timeout, Redis down, worker dying | 17 kinds of corrupted input across every supported type (noise, truncated, wrong header); LLM timeout and overload mapped as retryable, 3 retries with backoff then `failed`; **real** Celery client against a closed Redis port (upload fails in under 5 s with a clean 503, reads keep working); worker killed while reading pages, and between each pipeline stage, then job redelivered | `test_failures.py` (26), `test_tasks.py` (9) |
| 8 | Idempotency | each stage run 2-3 times (pages, extraction version, line items, validation findings, embeddings); the whole Celery task 3 times; same file uploaded again; same correction sent twice; reprocess adds versions but never duplicates inside one | `test_idempotency.py` (8), `test_tasks.py` |

Per file: search 52, API 51, field verification 50, validation rules 35, spreadsheets 32, OCR 32, security 26, failures 26, validation service 24, router 22, PDF 18, core utils 17, no-fabrication 16, extraction 15, LLM client 13, extraction pipeline 12, pipeline 10, segmenter 10, tasks 9, idempotency 8, mapper 7, embedding 7, schema 6, health 2.

## Problems the new tests found (all fixed, none hidden)

| Found by | Problem | Fix |
|---|---|---|
| OCR rotation test | Pages scanned upside down (180 degrees) or turned 90 degrees counter-clockwise were read as nonsense at 28-33% confidence | After a first read under 60% confidence, Tesseract's orientation detection is tried and the better read wins. Both now read at 94.8%. Dockerfile gets `tesseract-ocr-osd` |
| CSV encoding test | Windows-1251 (Russian) and Shift-JIS CSVs were decoded as Western text: chardet was right but unsure (confidence 0.17-0.35) and its answer was thrown away | The guess is used unless the file is almost all ASCII with a few odd bytes (like one "euro"), where cp1252 is the better bet |
| No-fabrication test | **"Ambiguous dates are not guessed" was enforced only in the prompt.** A model answering `2026-03-04` for the text `03/04/2026` was accepted | `verify_field` now compares the value with the dates written in its snippet; an ambiguous or contradicting date is set to null (`EXTRACT_DATE_NOT_IN_SNIPPET`) |
| Redis-down test | With Redis stopped, an upload request hung about **20 seconds** (Celery retrying its result backend) before failing | Task results are not stored (job state is in the database) and connection timeouts are short: now a 503 in about a second |
| Embedding test (earlier round) | Chunks could start in the middle of a word | Chunk starts snap to word boundaries |
| Failure review | Redis hands a dead worker's job to another worker only after `visibility_timeout` (default 1 hour); a longer OCR job would be started twice | Set to 4 hours, configurable (`CELERY_VISIBILITY_TIMEOUT_SECONDS`); see limits below |
| OCR crash test | When Tesseract crashed, the message shown was a meaningless `utf-8 codec can't decode` | Clear "OCR could not run on this image" error |

About ten tests I wrote were wrong at first (in this round and the search round before it) (bad test data such as a character cp1252 cannot encode, a miscounted newline, expecting 1.0 similarity to be rejected, a rounded score compared exactly, and similar). Each was corrected after checking the product was right; they are not listed as product bugs.

### Checked by breaking the code on purpose

For these changes, a deliberately broken version of the code made at least one test fail (so the tests really test them): date check removed; auto-rotation off; old encoding rule; Celery results back on; extraction no longer replacing its own version; name-in-snippet check off; rate limit; size limit while streaming; path stripping in filenames; reprocess allowed for any status; retrying permanent errors; RRF, similarity, filters and latest-version logic in search.

### One real-world experiment (not part of the suite)

Real Redis and a real Celery worker, a 30-page scanned PDF, `kill -9` on the worker after 25 pages were saved. The document was left in `processing`; a new worker started, the job came back within about 10 seconds (visibility timeout set to 15 s for the experiment), and it finished with 30 pages, 1 result, 30 embeddings and no duplicate pages.

## Known limits (tests document them; they are not failures)

- **A real value under the wrong label is accepted.** If the text says `Order number: ORD-555` and the model reports it as the invoice number, both the snippet and the value are genuinely in the text, so no code check can object. Covered by `test_known_limit_a_real_value_under_the_wrong_label...`. Only the prompt, the model and the downstream rules (duplicates, cross-document) help.
- **Tilt above about 15 degrees** is not corrected. The page reads with low confidence instead of being guessed (tested at 35 degrees).
- **OCR confidence is recorded but does not itself send a document to review.** A garbled page produces empty fields, which validation flags, but there is no rule on the page confidence.
- **The `hash` embedder captures shared words, not meaning**, so semantic search behaves like fuzzy keyword search until a real embedding model is added. The demo LLM is a regex stand-in.
- **Dead-worker recovery with Redis takes up to the visibility timeout** (4 hours by default). A document stuck in `processing` after a crash is also fixed by Reprocess. Two workers running the same job at the same moment (possible if a job outlives the timeout) are not prevented by a lock; data is protected by replacement-style writes and unique keys, but one of them may fail with an error.
- Upload rate limiting is per API process.
- Bengali OCR: only English and OSD language data is installed here; the Docker image installs Bengali but it was not run.

## Not covered by automated tests

- The web page (frontend): only a TypeScript type check; no browser tests.
- The real Anthropic API: no key was available. The adapter is tested against a fake SDK client, so a real call may still need adjustment.
- Docker images and `docker compose up`: Docker is not installed on this machine.
- Load and size: no 1,000-page OCR timing, no search benchmark at scale, no check that the HNSW index is used on big tables.
- A password-protected spreadsheet (a synthetic marker is tested, not a real encrypted workbook).
- Several workers racing on one document, and database outages.

## Coverage by file

| File | Statements | Covered | Lines not run |
|---|---|---|---|
| api/documents.py | 243 | 98.4% | 261, 280 |
| api/search.py, api/health.py, enqueue, validation_service (99.4%), pipeline (98.3%), pdf_processor, invoice_segmenter, workers/*, models/*, core/ratelimit, core/security, core/files, main | | 98-100% | validation 324 |
| services/search_service.py | 182 | 97.6% | 2 lines |
| services/ocr_processor.py | 138 | 98.2% | 3 lines (error branches) |
| services/llm_client.py | 117 | 98.5% | 2 lines |
| services/extraction_service.py | 223 | 96.1% | 162, 166, 214-215, 230, 312 |
| services/file_router.py | 88 | 96.8% | 2 lines |
| services/embedding_service.py | 94 | 95.2% | 110, 130 |
| services/field_verification.py | 134 | 95.5% | 62-63, 167-168, 188-189 |
| services/spreadsheet_mapper.py | 112 | 95.2% | 2 lines |
| core/config.py, core/errors.py | 58, 41 | 95% | 1-2 lines each |
| services/orchestrator.py | 42 | 94.0% | 45-46 (unexpected crash handler) |
| services/spreadsheet_processor.py | 176 | 91.0% | 62, 66, 119, 150-154, 184-191, 208, 237-238 (rare error branches) |
| prompts/__init__.py | 12 | 85.7% | 19 (malformed prompt file) |
| **Total** | **2,633** | **97.2%** | |
