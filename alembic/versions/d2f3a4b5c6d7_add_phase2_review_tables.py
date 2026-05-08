"""add phase2 review tables

Revision ID: d2f3a4b5c6d7
Revises: c1a2b3d4e5f6
Create Date: 2026-05-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "c1a2b3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(
            sa.Column("review_state", sa.String(length=32), nullable=False, server_default="new")
        )
        batch_op.add_column(
            sa.Column("mastery_score", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("recovery_stage", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("first_reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("forgot_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("shaky_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("got_it_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("fluent_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.alter_column("review_count", existing_type=sa.Integer(), server_default="0")
        batch_op.alter_column("again_count", existing_type=sa.Integer(), server_default="0")
        batch_op.alter_column("hard_count", existing_type=sa.Integer(), server_default="0")
        batch_op.alter_column("good_count", existing_type=sa.Integer(), server_default="0")
        batch_op.alter_column("easy_count", existing_type=sa.Integer(), server_default="0")
        batch_op.create_check_constraint(
            "ck_cards_review_state",
            "review_state IN ('new', 'strengthening', 'reviewing', 'mastered')",
        )
        batch_op.create_index("ix_cards_user_review_state", ["user_id", "review_state"])

    op.execute(
        """
        UPDATE cards
        SET
            forgot_count = again_count,
            shaky_count = hard_count,
            got_it_count = good_count,
            fluent_count = easy_count,
            first_reviewed_at = CASE
                WHEN review_count > 0 THEN last_reviewed_at
                ELSE first_reviewed_at
            END,
            review_state = CASE
                WHEN review_count <= 0 THEN 'new'
                WHEN last_review_result IN ('again', 'hard') THEN 'strengthening'
                WHEN last_review_result = 'easy' AND easy_count > 0 THEN 'mastered'
                ELSE 'reviewing'
            END,
            mastery_score = CASE
                WHEN review_count <= 0 THEN 0
                WHEN easy_count > 0 THEN 5
                WHEN good_count >= 3 THEN 4
                WHEN good_count >= 2 THEN 3
                ELSE 1
            END,
            recovery_stage = CASE
                WHEN last_review_result = 'again' THEN 2
                WHEN last_review_result = 'hard' THEN 1
                ELSE 0
            END
        """
    )

    with op.batch_alter_table("review_sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "started_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.add_column(sa.Column("batch_size", sa.Integer(), nullable=False, server_default="5"))
        batch_op.add_column(sa.Column("reviewed_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_index("ix_review_sessions_user_status", ["user_id", "status"])

    op.execute("UPDATE review_sessions SET reviewed_count = completed_count")

    with op.batch_alter_table("review_session_items") as batch_op:
        batch_op.add_column(sa.Column("result", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("reappear_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.drop_constraint("ck_review_session_items_status", type_="check")
        batch_op.create_check_constraint(
            "ck_review_session_items_status",
            "status IN ('pending', 'reviewed', 'done', 'skipped')",
        )

    op.create_table(
        "review_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("session_item_id", sa.Uuid(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_state_before", sa.String(length=32), nullable=False),
        sa.Column("review_state_after", sa.String(length=32), nullable=False),
        sa.Column("mastery_score_before", sa.Integer(), nullable=False),
        sa.Column("mastery_score_after", sa.Integer(), nullable=False),
        sa.Column("recovery_stage_before", sa.Integer(), nullable=False),
        sa.Column("recovery_stage_after", sa.Integer(), nullable=False),
        sa.Column("next_review_at_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_review_at_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "result IN ('forgot', 'shaky', 'got_it', 'fluent')",
            name="ck_review_logs_result",
        ),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"]),
        sa.ForeignKeyConstraint(["session_item_id"], ["review_session_items.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_logs_card_reviewed_at", "review_logs", ["card_id", "reviewed_at"])
    op.create_index("ix_review_logs_user_reviewed_at", "review_logs", ["user_id", "reviewed_at"])


def downgrade() -> None:
    op.drop_index("ix_review_logs_user_reviewed_at", table_name="review_logs")
    op.drop_index("ix_review_logs_card_reviewed_at", table_name="review_logs")
    op.drop_table("review_logs")

    with op.batch_alter_table("review_session_items") as batch_op:
        batch_op.drop_constraint("ck_review_session_items_status", type_="check")
        batch_op.create_check_constraint(
            "ck_review_session_items_status",
            "status IN ('pending', 'done', 'skipped')",
        )
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reappear_count")
        batch_op.drop_column("result")

    with op.batch_alter_table("review_sessions") as batch_op:
        batch_op.drop_index("ix_review_sessions_user_status")
        batch_op.drop_column("reviewed_count")
        batch_op.drop_column("batch_size")
        batch_op.drop_column("started_at")

    with op.batch_alter_table("cards") as batch_op:
        batch_op.drop_index("ix_cards_user_review_state")
        batch_op.drop_constraint("ck_cards_review_state", type_="check")
        batch_op.alter_column("easy_count", existing_type=sa.Integer(), server_default=None)
        batch_op.alter_column("good_count", existing_type=sa.Integer(), server_default=None)
        batch_op.alter_column("hard_count", existing_type=sa.Integer(), server_default=None)
        batch_op.alter_column("again_count", existing_type=sa.Integer(), server_default=None)
        batch_op.alter_column("review_count", existing_type=sa.Integer(), server_default=None)
        batch_op.drop_column("fluent_count")
        batch_op.drop_column("got_it_count")
        batch_op.drop_column("shaky_count")
        batch_op.drop_column("forgot_count")
        batch_op.drop_column("first_reviewed_at")
        batch_op.drop_column("recovery_stage")
        batch_op.drop_column("mastery_score")
        batch_op.drop_column("review_state")
