"""full-text search on pages, page number on embeddings

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("embeddings", sa.Column("page_number", sa.Integer))
    op.add_column("document_pages", sa.Column(
        "tsv", pg.TSVECTOR,
        sa.Computed("to_tsvector('simple', coalesce(extracted_text, ''))", persisted=True)))
    op.create_index("ix_document_pages_tsv", "document_pages", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_document_pages_tsv", "document_pages")
    op.drop_column("document_pages", "tsv")
    op.drop_column("embeddings", "page_number")
