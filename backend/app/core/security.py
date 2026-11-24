"""API authentication: one shared bearer token, checked in constant time.

Fails closed: outside ENVIRONMENT=development a missing token is a server error, never open access.
"""
import hmac

from fastapi import Header, HTTPException

from app.core.config import settings

MIN_TOKEN_LENGTH = 16


def check_configuration() -> None:
    """Called at startup so a misconfigured production server refuses to boot."""
    if settings.environment == "development":
        return
    if len(settings.api_auth_token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"API_AUTH_TOKEN must be set to at least {MIN_TOKEN_LENGTH} characters "
            "(for local use only, set ENVIRONMENT=development). Generate one with: openssl rand -hex 32"
        )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    token = settings.api_auth_token
    if not token:
        if settings.environment == "development":
            return
        raise HTTPException(503, "Server is misconfigured: API_AUTH_TOKEN is not set.")
    supplied = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else ""
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, "Invalid or missing token.", headers={"WWW-Authenticate": "Bearer"})
