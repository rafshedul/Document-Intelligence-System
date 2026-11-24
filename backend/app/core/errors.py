"""One JSON shape for every error: {"error": {"code", "message", "details"?, "request_id"}}.

Stack traces and exception text never leave the server; they go to the log with the request id.
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.errors")

CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 413: "file_too_large", 415: "unsupported_media_type", 422: "validation_error",
         429: "rate_limited", 503: "service_unavailable"}


def error_body(request: Request, status: int, message: str, code: str | None = None, details=None) -> dict:
    err = {"code": code or CODES.get(status, "error" if status < 500 else "internal_error"),
           "message": message, "request_id": getattr(request.state, "request_id", None)}
    if details is not None:
        err["details"] = details
    return {"error": err}


def register(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:12]
        try:
            response = await call_next(request)
        except Exception:  # last resort: a bug in a route or middleware
            log.exception("Unhandled error [%s] %s %s", request.state.request_id, request.method, request.url.path)
            response = JSONResponse(error_body(request, 500, "Internal server error."), status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message", "Request failed."))
            code = detail.get("code")  # a specific code (e.g. duplicate_file) beats the generic one for the status
            details = {k: v for k, v in detail.items() if k not in ("message", "code")} or None
        else:
            message, code, details = str(detail), None, None
        return JSONResponse(error_body(request, exc.status_code, message, code=code, details=details),
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
                   for e in exc.errors()]
        return JSONResponse(error_body(request, 422, "The request is not valid.", details=details), status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Unhandled error [%s] %s %s", getattr(request.state, "request_id", "-"),
                      request.method, request.url.path)
        return JSONResponse(error_body(request, 500, "Internal server error."), status_code=500)
