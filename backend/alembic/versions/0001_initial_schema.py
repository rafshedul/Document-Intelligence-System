"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

from app.core.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID_PK = dict(server_default=sa.text("gen_random_uuid()"), nullable=False)
NOW = sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("stored_path", sa.Text, nullable=False),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("file_size", sa.BigInteger, nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.CheckConstraint(
            "status IN ('queued','processing','completed','failed','needs_review')",
            name="document_status",
        ),
    )
    op.create_index("uq_documents_file_hash", "documents", ["file_hash"], unique=True)
    op.create_index("ix_documents_status", "documents", ["status"])

    op.create_table(
        "document_pages",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("extracted_text", sa.Text),
        sa.Column("extraction_method", sa.String(20), nullable=False),
        sa.Column("ocr_confidence", sa.Numeric(5, 2)),
        sa.PrimaryKeyConstraint("id", name="pk_document_pages"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE",
                                name="fk_document_pages_document_id_documents"),
        sa.UniqueConstraint("document_id", "page_number", name="uq_document_pages_document_id_page_number"),
        sa.CheckConstraint(
            "extraction_method IN ('digital','ocr','spreadsheet')",
            name="extraction_method",
        ),
    )

    op.create_table(
        "extracted_results",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_number", sa.String(128)),
        sa.Column("invoice_date", sa.Date),
        sa.Column("supplier_name", sa.String(512)),
        sa.Column("customer_name", sa.String(512)),
        sa.Column("subtotal", sa.Numeric(14, 2)),
        sa.Column("tax", sa.Numeric(14, 2)),
        sa.Column("total", sa.Numeric(14, 2)),
        sa.Column("currency", sa.String(3)),
        sa.Column("field_confidence", pg.JSONB),
        sa.Column("raw_llm_output", pg.JSONB),
        sa.Column("is_manually_corrected", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_extracted_results"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE",
                                name="fk_extracted_results_document_id_documents"),
        sa.UniqueConstraint("document_id", "version", name="uq_extracted_results_document_id_version"),
    )
    op.create_index("ix_extracted_results_invoice_number", "extracted_results", ["invoice_number"])

    op.create_table(
        "line_items",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("result_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("quantity", sa.Numeric(14, 3)),
        sa.Column("unit_price", sa.Numeric(14, 2)),
        sa.Column("line_total", sa.Numeric(14, 2)),
        sa.Column("position", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_line_items"),
        sa.ForeignKeyConstraint(["result_id"], ["extracted_results.id"], ondelete="CASCADE",
                                name="fk_line_items_result_id_extracted_results"),
        sa.UniqueConstraint("result_id", "position", name="uq_line_items_result_id_position"),
    )

    op.create_table(
        "validation_errors",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", pg.UUID(as_uuid=True)),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("field", sa.String(64)),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("id", name="pk_validation_errors"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE",
                                name="fk_validation_errors_document_id_documents"),
        sa.ForeignKeyConstraint(["result_id"], ["extracted_results.id"], ondelete="CASCADE",
                                name="fk_validation_errors_result_id_extracted_results"),
        sa.CheckConstraint("severity IN ('error','warning')", name="severity"),
    )
    op.create_index("ix_validation_errors_document_id", "validation_errors", ["document_id"])
    op.create_index("ix_validation_errors_result_id", "validation_errors", ["result_id"])

    op.create_table(
        "processing_history",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("details", pg.JSONB),
        sa.PrimaryKeyConstraint("id", name="pk_processing_history"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE",
                                name="fk_processing_history_document_id_documents"),
    )
    op.create_index("ix_processing_history_document_id_started_at", "processing_history",
                    ["document_id", "started_at"])

    op.create_table(
        "corrections",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("result_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Text),
        sa.Column("new_value", sa.Text),
        sa.Column("corrected_by", sa.String(255)),
        sa.Column("corrected_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_corrections"),
        sa.ForeignKeyConstraint(["result_id"], ["extracted_results.id"], ondelete="CASCADE",
                                name="fk_corrections_result_id_extracted_results"),
    )
    op.create_index("ix_corrections_result_id", "corrections", ["result_id"])

    op.create_table(
        "embeddings",
        sa.Column("id", pg.UUID(as_uuid=True), **UUID_PK),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(settings.embedding_dimension), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_embeddings"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE",
                                name="fk_embeddings_document_id_documents"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_embeddings_document_id_chunk_index"),
    )
    op.create_index(
        "ix_embeddings_embedding_hnsw", "embeddings", ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    for table in ("embeddings", "corrections", "processing_history", "validation_errors",
                  "line_items", "extracted_results", "document_pages", "documents"):
        op.drop_table(table)
    # The vector extension is left in place: other schemas may depend on it.
