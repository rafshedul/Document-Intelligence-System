import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey


class ProcessingHistory(UUIDPrimaryKey, Base):
    """One row per pipeline stage attempt (e.g. parse, ocr, extract, validate, embed)."""

    __tablename__ = "processing_history"
    # (document_id, started_at) serves the FK and "timeline for a document" queries.
    __table_args__ = (Index("ix_processing_history_document_id_started_at", "document_id", "started_at"),)

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))  # started | succeeded | failed | skipped
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSONB)
