"""add public material production trace fields

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "i3j4k5l6m7n8"
down_revision: str | Sequence[str] | None = "h2i3j4k5l6m7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("public_material_items", sa.Column("source", sa.String(length=80), nullable=True))
    op.add_column("public_material_items", sa.Column("source_id", sa.String(length=160), nullable=True))
    op.add_column("public_material_items", sa.Column("license", sa.String(length=120), nullable=True))
    op.add_column("public_material_items", sa.Column("corpus_rank", sa.Integer(), nullable=True))
    op.add_column("public_material_items", sa.Column("corpus_frequency", sa.Float(), nullable=True))
    op.add_column("public_material_items", sa.Column("production_batch", sa.String(length=80), nullable=True))
    op.create_index("ix_public_material_items_source", "public_material_items", ["source", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_public_material_items_source", table_name="public_material_items")
    op.drop_column("public_material_items", "production_batch")
    op.drop_column("public_material_items", "corpus_frequency")
    op.drop_column("public_material_items", "corpus_rank")
    op.drop_column("public_material_items", "license")
    op.drop_column("public_material_items", "source_id")
    op.drop_column("public_material_items", "source")
