# REST API

Interactive docs (Swagger UI): `http://localhost:8000/docs`. Click **Authorize** and paste the token. In development without `API_AUTH_TOKEN` no token is needed.

All `/api` routes need `Authorization: Bearer <API_AUTH_TOKEN>`. Every error has the same shape:

```json
{"error": {"code": "not_found", "message": "Document not found.", "request_id": "e2a9006f49d2", "details": null}}
```

| Method and path | What it does | Main errors |
|---|---|---|
| `POST /api/documents/upload` | Upload one or more files (form field `files`, repeat it). Saves each, hashes it, creates a `queued` record, sends a Celery job, returns the ids at once. No OCR or AI in the request. Each file succeeds or fails on its own; if all fail you get the first error with all results in `error.details.files`. | 400 empty / too many files, 409 `duplicate_file`, 413 `file_too_large`, 415 `unsupported_format` / `extension_mismatch`, 429 rate limit, 503 queue down |
| `GET /api/documents` | List. Filters: `status`, `file_type`, `flagged`, `created_from`, `created_to`. Pagination: `page`, `page_size` (max 100). Returns `items`, `total`, `page`, `page_size`, `pages`. | 422 bad parameter |
| `GET /api/documents/{id}/status` | `status`, current `stage` (`queued`, a stage name, or `done`), `progress` 0-100, `error_message`, `attempts`. | 404 |
| `GET /api/documents/{id}/result` | Structured invoices (fields with confidence and source snippet, line items), validation `findings`, `history` of every stage. | 404 |
| `PATCH /api/documents/{id}/result` | Manual correction. Body: `{"invoice_index": 0, "fields": {"total": "105.00"}, "line_items": [{"position": 1, "line_total": "60.00"}], "corrected_by": "name"}`. Each change is logged in `corrections` (old value, new value, who, when), validation runs again, status is updated. | 400 nothing changed, 404, 409 still processing, 422 invalid value |
| `POST /api/documents/{id}/reprocess` | Queue a `failed` or `needs_review` document again. The attempt number is recorded in `processing_history` (`stage = enqueue`). A new extraction version is created; manual corrections stay in the older version and in the `corrections` log. | 404, 409 wrong status, 503 |
| `DELETE /api/documents/{id}` | Delete the record, its file and everything derived from it. 204 on success. | 404, 409 while processing |
| `GET /api/documents/{id}` | The result plus extracted page text (used by the web page). | 404 |

## Background job (Celery)

One task, `process_document`, runs: **file check and text extraction (including OCR)**, **LLM extraction**, **validation**, **embeddings**. Progress (0-100) and the history row of each stage are updated as it goes; a large PDF reports progress page batch by page batch.

| Stage | Progress |
|---|---|
| `validate_file` | 0-5 |
| `extract_text` (PDF, OCR, spreadsheets) | 5-50 |
| `segment`, `extract_fields`, `save_results` | 50-85 |
| `validate` | 85-95 |
| `embed` | 95-100 |

- **Retries.** Temporary problems (LLM unreachable or overloaded after its own retries, unexpected errors) retry with exponential backoff: 5, 10, 20 seconds (`TASK_RETRY_BACKOFF_SECONDS`), at most 3 times (`TASK_MAX_RETRIES`). Each retry is a `retry` row in the history; after the last one a `retry_exhausted` row is added and the status stays `failed`. Permanent problems (corrupted file, wrong password, bad API key) are not retried.
- **Idempotent.** The job carries its attempt number. Running the same attempt again replaces pages, extraction results of that attempt, and embeddings instead of adding more. A job whose document was deleted is skipped.
- **Embeddings.** `EMBEDDING_PROVIDER=hash` is a built-in lexical embedder for demos, not a semantic model. A failure in this stage is logged and does not change the document's status.
- `TASK_BACKEND=inline` runs the same pipeline in a FastAPI background task (no Redis, no retries); for local development only.
