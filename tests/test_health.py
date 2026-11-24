import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_ok():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchone.side_effect = [(1,), (1,)]
    with patch("app.api.health.psycopg.connect", return_value=conn), patch(
        "app.api.health.redis.Redis.from_url"
    ) as r:
        r.return_value.ping.return_value = True
        res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_health_degraded_when_deps_down():
    with patch("app.api.health.psycopg.connect", side_effect=Exception("down")), patch(
        "app.api.health.redis.Redis.from_url", side_effect=Exception("down")
    ):
        res = client.get("/health")
    assert res.status_code == 503
    assert res.json()["status"] == "degraded"
