
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPrimaryKey
from app.models.document import _enum
from app.models.enums import Severity

# Money is always NUMERIC(14,2); never float.
Money = Numeric(14, 2)


class ExtractedResult(UUIDPrimaryKey, Base):
    __tablename__ = "extracted_results"
    __table_args__ = (
        # One document can hold many invoices (invoice_index) and many extraction runs (version).
        # This also serves as the FK index on document_id.
        UniqueConstraint("document_id", "version", "invoice_index",
                         name="uq_extracted_results_document_id_version_invoice_index"),
        Index("ix_extracted_results_invoice_number", "invoice_number"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    invoice_index: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    supplier_name: Mapped[str | None] = mapped_column(String(512))
    customer_name: Mapped[str | None] = mapped_column(String(512))
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    tax: Mapped[Decimal | None] = mapped_column(Money)
    total: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))  # ISO 4217
    field_confidence: Mapped[dict | None] = mapped_column(JSONB)
    raw_llm_output: Mapped[dict | None] = mapped_column(JSONB)
    is_manually_corrected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    line_items: Mapped[list["LineItem"]] = relationship(
        back_populates="result", cascade="all, delete-orphan", passive_deletes=True,
        order_by="LineItem.position",
    )


class LineItem(UUIDPrimaryKey, Base):
    __tablename__ = "line_items"
    # (result_id, position) also serves as the FK index on result_id.
    __table_args__ = (
        UniqueConstraint("result_id", "position", name="uq_line_items_result_id_position"),
    )

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_results.id", ondelete="CASCADE")
    )
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))  # may be fractional
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    line_total: Mapped[Decimal | None] = mapped_column(Money)
    position: Mapped[int] = mapped_column(Integer)

    result: Mapped[ExtractedResult] = relationship(back_populates="line_items")


class ValidationError(UUIDPrimaryKey, Base):
    __tablename__ = "validation_errors"
    __table_args__ = (
        Index("ix_validation_errors_document_id", "document_id"),
        Index("ix_validation_errors_result_id", "result_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_results.id", ondelete="CASCADE")
    )
    rule_code: Mapped[str] = mapped_column(String(64))
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"))
    field: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Correction(UUIDPrimaryKey, Base):
    __tablename__ = "corrections"
    __table_args__ = (Index("ix_corrections_result_id", "result_id"),)

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extracted_results.id", ondelete="CASCADE")
    )
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    # No auth yet; nullable until user accounts exist.
    corrected_by: Mapped[str | None] = mapped_column(String(255))
    corrected_at: Mapped[datetime] = mapped_column(server_default=func.now())
