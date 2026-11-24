# Running locally (without Docker)

One-time setup (already done on this machine): `backend/.venv` holds the Python packages (`pip install -r backend/requirements.txt python-multipart`), `cd frontend && npm install`. Tesseract: `brew install tesseract`. Postgres with pgvector must be running and migrated (`alembic upgrade head`).

```bash
# Terminal 1: backend  (http://localhost:8000/docs)
cd backend
export DATABASE_URL=postgresql://docintel@127.0.0.1:5544/docintel REDIS_URL=redis://localhost:6379/0
export ENVIRONMENT=development LLM_PROVIDER=demo UPLOAD_DIR=/tmp/docintel-uploads   # demo = rule-based extractor, no API key; development = no login
.venv/bin/uvicorn app.main:app --reload --port 8000

# Terminal 2: frontend (http://localhost:3000)
cd frontend && npm run dev

# Tests
TEST_DATABASE_URL=postgresql+psycopg://docintel@127.0.0.1:5544/docintel backend/.venv/bin/python -m pytest tests -q
```

`LLM_PROVIDER=anthropic` with `LLM_API_KEY` set uses Claude. The `demo` provider only understands simple "Label: value" invoices (see `services/demo_llm.py`).

The upload API runs the pipeline (process, extract, validate) in a background task, so Redis is not needed yet; Celery will replace that.

## With Redis and a Celery worker (as in Docker)

```bash
brew install redis && redis-server                       # terminal A
cd backend && export DATABASE_URL=... REDIS_URL=redis://localhost:6379/0 LLM_PROVIDER=demo UPLOAD_DIR=/tmp/docintel-uploads
.venv/bin/celery -A app.workers.celery_app.celery_app worker --loglevel=info --concurrency=2   # terminal B
TASK_BACKEND=celery ENVIRONMENT=production API_AUTH_TOKEN=$(openssl rand -hex 24) .venv/bin/uvicorn app.main:app --port 8000   # terminal C
```
In production mode the web page asks for the token (`API_AUTH_TOKEN`); `/docs` is disabled.
