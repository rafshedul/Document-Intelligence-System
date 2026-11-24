from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import documents, health
from app.core import security
from app.core.config import settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    security.check_configuration()  # refuse to start unauthenticated outside development
    yield


def create_app() -> FastAPI:
    dev = settings.environment == "development"
    app = FastAPI(
        title="Document Intelligence System", lifespan=lifespan,
        docs_url="/docs" if dev else None, redoc_url=None, openapi_url="/openapi.json" if dev else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
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

    app.include_router(health.router)
    app.include_router(documents.router)
    return app


app = create_app()
