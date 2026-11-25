# Web interface

Next.js 14 (App Router), TypeScript, Tailwind CSS. Code: `frontend/`. No UI library; the only runtime dependencies are Next and React.

## Pages

| Page | What it does |
|---|---|
| `/upload` | Drag and drop (or choose) many files at once. Type and size are checked in the browser before anything is sent; each file is uploaded on its own with its own progress bar (3 at a time), and shows Uploaded / Already uploaded (link to the existing document) / the reason it failed, with Retry for failures. |
| `/documents` | List with name, type, status badge, date; filters by status and type; pagination. While any document is queued or processing, the list asks the server every 2 seconds and shows a live progress bar and stage, without reloading. Table on wide screens, cards on phones. |
| `/documents/[id]` | Left: the original file (PDF or image; spreadsheets and TIFF offer a download). Right: extracted fields with the confidence next to each (yellow below 70%), the source text each value came from, and a line item table. Validation panel (errors red, warnings amber; each finding names its field and jumps to it). Edit mode, Reprocess, Delete, correction history. Progress while processing. |
| `/search` | Search box, Hybrid / Keyword / Semantic toggle, filters for supplier, invoice date range and status. Results show the matched snippet with the words highlighted, page number, match type and score. The query and filters live in the URL, so refresh and the back button keep them. |

Every page has loading, error (with a Try again button) and empty states.

## Editing

Click any value (or press **Edit values**) to edit; change several fields and line item cells, then **Save**. Only what changed is sent (`PATCH /api/documents/{id}/result`). Amounts, dates and currency are checked in the browser first with the same rules as the server, and anything the server still refuses is shown next to the field. After saving, validation runs again on the server and the panel updates; every change is logged under **Correction history**. Reprocessing a document that was edited by hand asks for confirmation, because the new extraction does not contain the edits (they stay in the older version and in the log).

## Configuration

The API address comes from an environment variable, `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`). Next.js fixes `NEXT_PUBLIC_*` values into the page **when it is built**, so set it before `npm run build` (in development, in `frontend/.env.local`; see `frontend/.env.example`). In Docker it is a build argument of the frontend image (`NEXT_PUBLIC_API_URL` in the project's `.env`) and must be the address the *browser* uses to reach the API. The API's `CORS_ORIGINS` must include the page's address.

If the API wants a token (`ENVIRONMENT=production`), the app shows a sign-in box; the token is kept for the tab only (`sessionStorage`) and a token that stops working sends you back to it.

## Structure

```
app/            pages (layout, upload, documents, documents/[id], search)
components/     AuthGate, Nav, FilePreview, FieldsPanel, ValidationPanel, CorrectionHistory, Highlighted, ui (badges, progress, states)
lib/api.ts      the typed API client: URLs, token, the uniform error format, upload with progress
lib/types.ts    response types, mirroring the backend models
lib/edits.ts    what changed -> PATCH body; client-side validation; which row a finding belongs to
lib/format.ts, lib/upload.ts, lib/hooks.ts   formatting and highlight splitting; file checks and a small upload pool; polling
```

Preview files are fetched with the token and shown from a `blob:` URL (an `<iframe src>` cannot send an Authorization header). The server sends them with a Content-Security-Policy that forbids scripts, and their type comes from what the file was detected as at upload. Search snippets arrive as plain text plus highlight offsets and are rendered as React text, never as HTML.

## Tests

```bash
cd frontend
npm run typecheck && npm run lint && npm test       # 65 unit tests (vitest): API client, edit diff and validation, upload checks, highlight splitting
npm run build
```
Browser tests that drive the real page in Chromium (upload, live progress, edit and save, reprocess, search, sign-in, phone-sized screens, error states) are in `tests/e2e/`; see its README.

## Limits

- The PDF preview needs a browser that can show PDFs inline; there is an "Open in a new tab" link for those that cannot (many phone browsers). TIFF is not previewed.
- A line item table has no confidence column: the API does not return one per line item.
- On phones the line item table scrolls sideways.
- Only the token sign-in exists; there are no user accounts.
