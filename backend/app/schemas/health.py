from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    postgres: bool
    pgvector: bool
    redis: bool
