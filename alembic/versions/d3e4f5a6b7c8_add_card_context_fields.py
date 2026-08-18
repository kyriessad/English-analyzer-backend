"""add structured source context and example fields to cards

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-08-14 16:30:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(sa.Column("source_context", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("source_url", sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column("example_sentence", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("example_translation", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_column("example_translation")
        batch_op.drop_column("example_sentence")
        batch_op.drop_column("source_url")
        batch_op.drop_column("source_context")
