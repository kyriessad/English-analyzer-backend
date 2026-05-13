"""add card_snapshot to review_logs

Revision ID: a6b7c8d9e0f1
Revises: f4a5b6c7d8e9
Create Date: 2026-05-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "review_logs",
        sa.Column("card_snapshot", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_logs", "card_snapshot")
