"""documents.stage and documents.progress

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("stage", sa.String(64)))
    op.add_column("documents", sa.Column("progress", sa.Integer, nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("documents", "progress")
    op.drop_column("documents", "stage")
