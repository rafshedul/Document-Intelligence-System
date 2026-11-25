# Browser tests (optional)

Playwright scripts that drive the real web page in a headless Chromium and check what a person would see.
They are **not** part of `pytest tests` (they need running servers and a browser); run them by hand.

```bash
python3 -m venv /tmp/e2e-venv && /tmp/e2e-venv/bin/pip install playwright && /tmp/e2e-venv/bin/playwright install chromium
```

Start the stack as in `docs/running_locally.md` (backend on :8000 with `ENVIRONMENT=development`, `LLM_PROVIDER=demo`, `TASK_BACKEND=inline`; frontend `npm run dev` on :3000; Tesseract installed) on an empty database, then:

```bash
/tmp/e2e-venv/bin/python tests/e2e/ui_upload_and_list.py          # upload + list
/tmp/e2e-venv/bin/python tests/e2e/ui_detail_edit_search.py       # detail, edit, reprocess, previews, search (run after the first)
```

`ui_login_mobile_errors.py` also needs a second, protected stack, plus it stops the normal API at the end:

```bash
cd frontend && NEXT_DIST_DIR=.next-prod NEXT_PUBLIC_API_URL=http://localhost:8001 npm run build
NEXT_DIST_DIR=.next-prod npm run start -- -p 3001 &
cd ../backend && ENVIRONMENT=production API_AUTH_TOKEN=prod-token-0123456789abcdef CORS_ORIGINS=http://localhost:3001 \
    TASK_BACKEND=inline .venv/bin/uvicorn app.main:app --port 8001 &
/tmp/e2e-venv/bin/python tests/e2e/ui_login_mobile_errors.py
```

Each check prints `PASS`/`FAIL`; the exit code is 1 if anything failed. Screenshots go to `$E2E_SHOTS` (default `/tmp/docintel-e2e-shots`).
Environment variables: `E2E_BASE` (default `http://localhost:3000`), `E2E_SAMPLES` (default `sample_data/`).
Note: the PDF preview is blank in headless Chromium (it has no PDF viewer); the scripts check that the preview frame is loaded from an authorised blob instead.
