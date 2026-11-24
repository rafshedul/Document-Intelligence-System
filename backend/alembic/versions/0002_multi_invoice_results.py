"""several invoices per document

A document (e.g. a 1000-page PDF) can contain many invoices, so a result is now
identified by (document_id, version, invoice_index) and remembers its page range.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extracted_results", sa.Column("invoice_index", sa.Integer, nullable=False, server_default=sa.text("0")))
    op.add_column("extracted_results", sa.Column("page_start", sa.Integer))
    op.add_column("extracted_results", sa.Column("page_end", sa.Integer))
    op.drop_constraint("uq_extracted_results_document_id_version", "extracted_results", type_="unique")
    op.create_unique_constraint("uq_extracted_results_document_id_version_invoice_index", "extracted_results",
                                ["document_id", "version", "invoice_index"])


def downgrade() -> None:
    # Only safe while every document has a single invoice per version.
    op.drop_constraint("uq_extracted_results_document_id_version_invoice_index", "extracted_results", type_="unique")
    op.create_unique_constraint("uq_extracted_results_document_id_version", "extracted_results",
                                ["document_id", "version"])
    op.drop_column("extracted_results", "page_end")
    op.drop_column("extracted_results", "page_start")
    op.drop_column("extracted_results", "invoice_index")
