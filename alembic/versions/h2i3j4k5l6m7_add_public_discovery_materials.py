"""add public discovery materials and per-user known state

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "h2i3j4k5l6m7"
down_revision: str | Sequence[str] | None = "g1h2i3j4k5l6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "public_material_packs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('word_book', 'expression', 'daily_quote')", name="ck_public_material_packs_kind"),
        sa.CheckConstraint("status IN ('active', 'hidden')", name="ck_public_material_packs_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_public_material_packs_code"),
    )
    op.create_index("ix_public_material_packs_status_sort", "public_material_packs", ["status", "sort_order"])

    op.create_table(
        "public_material_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_normalized", sa.Text(), nullable=False),
        sa.Column("chinese", sa.Text(), nullable=False),
        sa.Column("card_type", sa.String(length=16), nullable=False),
        sa.Column("source_label", sa.String(length=120), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.String(length=240), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("card_type IN ('word', 'phrase', 'sentence')", name="ck_public_material_items_card_type"),
        sa.CheckConstraint("position > 0", name="ck_public_material_items_position_positive"),
        sa.CheckConstraint("status IN ('approved', 'hidden')", name="ck_public_material_items_status"),
        sa.ForeignKeyConstraint(["pack_id"], ["public_material_packs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pack_id", "content_normalized", name="uq_public_material_items_pack_content"),
        sa.UniqueConstraint("pack_id", "position", name="uq_public_material_items_pack_position"),
    )
    op.create_index("ix_public_material_items_content_normalized", "public_material_items", ["content_normalized"])
    op.create_index("ix_public_material_items_pack_status_position", "public_material_items", ["pack_id", "status", "position"])

    op.create_table(
        "user_material_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("material_item_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('known')", name="ck_user_material_states_state"),
        sa.ForeignKeyConstraint(["material_item_id"], ["public_material_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "material_item_id", name="uq_user_material_states_user_item"),
    )
    op.create_index("ix_user_material_states_user_state", "user_material_states", ["user_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_user_material_states_user_state", table_name="user_material_states")
    op.drop_table("user_material_states")
    op.drop_index("ix_public_material_items_pack_status_position", table_name="public_material_items")
    op.drop_index("ix_public_material_items_content_normalized", table_name="public_material_items")
    op.drop_table("public_material_items")
    op.drop_index("ix_public_material_packs_status_sort", table_name="public_material_packs")
    op.drop_table("public_material_packs")
