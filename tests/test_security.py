"""Authentication and hardening; no database needed."""
import pytest
from fastapi.testclient import TestClient

from app.core import security
from app.core.config import settings
from app.main import create_app

TOKEN = "t" * 32


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    return TestClient(create_app())


ID = "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/info"), ("GET", "/api/search?q=x"), ("GET", "/api/documents"), ("POST", "/api/documents/upload"),
    ("GET", f"/api/documents/{ID}"), ("GET", f"/api/documents/{ID}/status"), ("GET", f"/api/documents/{ID}/result"),
    ("PATCH", f"/api/documents/{ID}/result"), ("POST", f"/api/documents/{ID}/reprocess"),
    ("DELETE", f"/api/documents/{ID}"),
])
def test_every_api_endpoint_requires_the_token(prod, method, path):
    r = prod.request(method, path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"  # not 404/422: auth comes first


@pytest.mark.parametrize("header", ["Bearer wrong-token", "Bearer", "Basic " + TOKEN, TOKEN, "bearer ", ""])
def test_wrong_or_malformed_tokens_are_rejected(prod, header):
    r = prod.get("/api/info", headers={"Authorization": header})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_right_token_is_accepted(prod):
    r = prod.get("/api/info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["llm_provider"]
    assert prod.get("/api/info", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_health_stays_open_for_monitoring(prod):
    assert prod.get("/health").status_code in (200, 503)  # reachable without a token


def test_production_without_a_token_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", "")
    client = TestClient(create_app())
    assert client.get("/api/info").status_code == 503  # never open access
    with pytest.raises(RuntimeError, match="API_AUTH_TOKEN"):
        security.check_configuration()  # and the server refuses to start


def test_short_tokens_are_refused_at_startup(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_auth_token", "short")
    with pytest.raises(RuntimeError):
        security.check_configuration()


def test_development_without_a_token_is_open(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "api_auth_token", "")
    security.check_configuration()
    assert TestClient(create_app()).get("/api/info").status_code == 200


def test_a_configured_token_is_enforced_even_in_development(monkeypatch):
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "api_auth_token", TOKEN)
    assert TestClient(create_app()).get("/api/info").status_code == 401


def test_api_docs_are_on_but_the_endpoints_stay_protected(prod):
    assert prod.get("/docs").status_code == 200 and prod.get("/openapi.json").status_code == 200
    assert prod.get("/api/documents").status_code == 401


def test_security_headers_and_no_caching(prod):
    r = prod.get("/api/info", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_cors_only_for_configured_origins(prod):
    ok = prod.options("/api/documents", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = prod.options("/api/documents", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers
    allowed = ok.headers.get("access-control-allow-methods", "")
    assert "PATCH" in allowed and "DELETE" in allowed and "PUT" not in allowed


def test_token_comparison_is_constant_time():
    import inspect
    assert "compare_digest" in inspect.getsource(security.require_auth)
