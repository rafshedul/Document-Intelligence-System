from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import documents, health
from app.core import errors, security
from app.core.config import settings

DESCRIPTION = """Upload invoices (PDF, scans, spreadsheets), get structured data with validation.

Authorize with the **API_AUTH_TOKEN** (button below). Errors always look like
`{"error": {"code", "message", "details?", "request_id"}}`."""


@asynccontextmanager
async def lifespan(_: FastAPI):
    security.check_configuration()  # refuse to start unauthenticated outside development
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Document Intelligence System", version="0.1.0", description=DESCRIPTION, lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None, redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    errors.register(app)  # registered last so the request id wraps everything
    app.include_router(health.router)
    app.include_router(documents.router)
    return app


app = create_app()
