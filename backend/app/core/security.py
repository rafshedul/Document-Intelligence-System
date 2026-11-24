"""API authentication: one shared bearer token (an API key), checked in constant time.

Fails closed: outside ENVIRONMENT=development a missing token is a server error, never open access.
"""
import hmac

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

MIN_TOKEN_LENGTH = 16
# auto_error=False so we return our own JSON error; the scheme still shows up in the OpenAPI docs
# (the "Authorize" button in Swagger UI).
bearer = HTTPBearer(auto_error=False, description="API_AUTH_TOKEN")


def check_configuration() -> None:
    """Called at startup so a misconfigured production server refuses to boot."""
    if settings.environment == "development":
        return
    if len(settings.api_auth_token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"API_AUTH_TOKEN must be set to at least {MIN_TOKEN_LENGTH} characters "
            "(for local use only, set ENVIRONMENT=development). Generate one with: openssl rand -hex 32"
        )


def require_auth(credentials: HTTPAuthorizationCredentials | None = Security(bearer)) -> None:
    token = settings.api_auth_token
    if not token:
        if settings.environment == "development":
            return
        raise HTTPException(503, "Server is misconfigured: API_AUTH_TOKEN is not set.")
    supplied = credentials.credentials.strip() if credentials and credentials.scheme.lower() == "bearer" else ""
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, "Invalid or missing token.", headers={"WWW-Authenticate": "Bearer"})
