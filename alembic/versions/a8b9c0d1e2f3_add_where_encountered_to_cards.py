"""add where_encountered to cards

Revision ID: a8b9c0d1e2f3
Revises: f4a5b6c7d8e9
Create Date: 2026-05-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    card_columns = _columns("cards")
    with op.batch_alter_table("cards") as batch_op:
        if "where_encountered" not in card_columns:
            batch_op.add_column(sa.Column("where_encountered", sa.Text(), nullable=True))


def downgrade() -> None:
    card_columns = _columns("cards")
    with op.batch_alter_table("cards") as batch_op:
        if "where_encountered" in card_columns:
            batch_op.drop_column("where_encountered")
