
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, Computed, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import DocumentStatus, ExtractionMethod


def _enum(cls: type, name: str) -> Enum:
    # VARCHAR + CHECK instead of a native PG enum: adding a value later is a
    # simple constraint change, not an ALTER TYPE.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [m.value for m in e],
    )


class Document(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("uq_documents_file_hash", "file_hash", unique=True),
        Index("ix_documents_status", "status"),
    )

    original_filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(Text)
    file_type: Mapped[str] = mapped_column(String(32))
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 hex
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, "document_status"),
        default=DocumentStatus.queued,
        server_default=DocumentStatus.queued.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    # Where the pipeline is right now, and how far along (0-100). stage is "done" when finished.
    stage: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Completed, but with unresolved validation warnings a reviewer should look at.
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentPage(UUIDPrimaryKey, Base):
    __tablename__ = "document_pages"
    # (document_id, page_number) also serves as the FK index on document_id.
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_document_pages_document_id_page_number"),
        Index("ix_document_pages_tsv", "tsv", postgresql_using="gin"),  # keyword search
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    page_number: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        _enum(ExtractionMethod, "extraction_method")
    )
    # Mean OCR confidence 0-100 (Tesseract scale); NULL for digital/spreadsheet.
    ocr_confidence: Mapped[float | None] = mapped_column(Numeric(5, 2))
    # Full-text vector kept in sync by the database. 'simple' = no stemming or stop words, so it works
    # for any language (English, Bengali...). Not loaded unless asked for.
    tsv = mapped_column(TSVECTOR, Computed("to_tsvector('simple', coalesce(extracted_text, ''))", persisted=True),
                        deferred=True)

    document: Mapped[Document] = relationship(back_populates="pages")
