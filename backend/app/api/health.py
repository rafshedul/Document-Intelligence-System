import psycopg
import redis
from fastapi import APIRouter, Response

from app.core.config import settings
from app.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    pg = vec = rd = False
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            pg = conn.execute("SELECT 1").fetchone() == (1,)
            vec = conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone() is not None
    except Exception:
        pass
    try:
        rd = bool(redis.Redis.from_url(settings.redis_url, socket_timeout=3).ping())
    except Exception:
        pass
    ok = pg and vec and rd
    if not ok:
        response.status_code = 503
    return HealthResponse(
        status="ok" if ok else "degraded", postgres=pg, pgvector=vec, redis=rd
    )
