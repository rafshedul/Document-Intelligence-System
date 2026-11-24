# Security

## What is in place

| Area | Measure |
|---|---|
| Authentication | Every `/api` route needs `Authorization: Bearer <API_AUTH_TOKEN>`. The token is compared in constant time (`hmac.compare_digest`). `/health` is open for monitoring and reveals only whether Postgres, pgvector and Redis answer. |
| Fail closed | Outside `ENVIRONMENT=development`, the server refuses to start unless `API_AUTH_TOKEN` is set (16+ characters). A running server with no token answers 503, never open access. |
| Secrets | `.env` is git-ignored; only `.env.example` (placeholders) is committed. LLM keys are read from the environment, never logged or returned by the API. |
| Uploads | Allow-list of extensions plus magic-byte check (a mislabelled file is rejected); size limit enforced while streaming; stored under a random name, never the user's filename or path; files are never executed; exact duplicates (SHA-256) are rejected. |
| Untrusted text | Document text is wrapped in tags and the prompt tells the model to ignore instructions inside it. Model output is schema-validated and every value must be traceable to the text, so injected instructions cannot add data that is not in the document. The model has no tools and no write access. |
| Errors | Users see short messages. Tracebacks go to the server log and `processing_history`, not to API responses. |
| Browser | CORS limited to `CORS_ORIGINS` and to GET/POST; `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on API responses. The token is kept in `sessionStorage` (cleared when the tab closes). |
| Surface | Swagger UI (`/docs`) is on by default and can be hidden with `DOCS_ENABLED=false`; every endpoint behind it still needs the token. Postgres and Redis are not published to the host by `docker-compose.yml`. |
| Rate limiting | Uploads: `UPLOAD_RATE_LIMIT_PER_MINUTE` files per client IP (default 60), 429 with `Retry-After`. In memory per process: with several API replicas each counts separately, so add a limit at the reverse proxy too. |
| Error format | Every error is `{"error": {code, message, details?, request_id}}`. Stack traces and exception text never reach the client; they are logged with the same `request_id` (also sent as the `X-Request-ID` header). |
| Filenames | The display name keeps only the last path component and drops control characters; files are stored under a random name, so `../../x` cannot reach another folder. |

## Not done yet (do these before real use)

- **No user accounts.** One shared token means everyone who has it sees every document. Needed: users, password hashing or SSO, roles, and `owner/tenant` columns enforced in every query.
- **No brute-force protection** on the token itself (only uploads are rate limited); put the API behind a reverse proxy that rate-limits every route.
- **No TLS.** Serve behind HTTPS (the token and the invoices travel in clear text otherwise).
- `sessionStorage` tokens can be read by any script on the page; a cookie-based session (HttpOnly) is the better design once accounts exist.
- Invoices contain financial and personal data and are sent to the LLM provider: review the provider's retention terms. Encrypt disks and backups.
- No antivirus scan of uploads; no audit log of who viewed what.
- The default Postgres password in `.env.example` is `change-me`; change it.
- Dependencies are pinned but not scanned; run `pip-audit` / `npm audit` in CI. (`next@14.2.18` has a known advisory: upgrade to a patched 14.2.x.)

## If a secret leaks

Deleting the file is not enough: git history keeps it. Revoke the key at the provider, create a new one, then (optionally) rewrite history.
