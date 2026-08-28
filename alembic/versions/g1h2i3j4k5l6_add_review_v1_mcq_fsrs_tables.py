"""add review v1 mcq fsrs tables

Revision ID: g1h2i3j4k5l6
Revises: f6a7b8c9d0e1
Create Date: 2026-08-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "g1h2i3j4k5l6"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "card_lexical_metadata",
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("content_normalized", sa.Text(), nullable=False),
        sa.Column("edict_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pos", sa.String(length=32), nullable=True),
        sa.Column("frq", sa.Integer(), nullable=True),
        sa.Column("bnc", sa.Integer(), nullable=True),
        sa.Column("metadata_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("card_id"),
    )
    op.create_index("ix_card_lexical_metadata_content_normalized", "card_lexical_metadata", ["content_normalized"])
    op.create_index("ix_card_lexical_metadata_pos_frq", "card_lexical_metadata", ["pos", "frq"])
    op.create_index("ix_card_lexical_metadata_pos_bnc", "card_lexical_metadata", ["pos", "bnc"])

    op.create_table(
        "card_fsrs_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("state", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stability", sa.Float(), nullable=True),
        sa.Column("difficulty", sa.Float(), nullable=True),
        sa.Column("fsrs_card_json", json_type, nullable=False),
        sa.Column("scheduler_name", sa.String(length=64), nullable=False, server_default="py-fsrs"),
        sa.Column("scheduler_version", sa.String(length=32), nullable=False, server_default="6.3.2"),
        sa.Column("scheduler_parameters", json_type, nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("state IN (1, 2, 3)", name="ck_card_fsrs_states_state"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_id", name="uq_card_fsrs_states_card_id"),
    )
    op.create_index("ix_card_fsrs_states_card_due", "card_fsrs_states", ["card_id", "due_at"])
    op.create_index("ix_card_fsrs_states_user_due", "card_fsrs_states", ["user_id", "due_at"])

    op.create_table(
        "review_mcq_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("session_item_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=False),
        sa.Column("parent_question_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_repeat", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("card_version", sa.Integer(), nullable=False),
        sa.Column("prompt_content", sa.Text(), nullable=False),
        sa.Column("prompt_content_normalized", sa.Text(), nullable=False),
        sa.Column("correct_answer", sa.Text(), nullable=False),
        sa.Column("correct_answer_source", sa.String(length=32), nullable=False, server_default="understanding"),
        sa.Column("options_snapshot", json_type, nullable=False),
        sa.Column("option_order", json_type, nullable=False),
        sa.Column("correct_option_id", sa.String(length=64), nullable=False),
        sa.Column("generation_version", sa.String(length=32), nullable=False, server_default="mcq-v1"),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("attempt_no IN (1, 2)", name="ck_review_mcq_questions_attempt_no"),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"]),
        sa.ForeignKeyConstraint(["parent_question_id"], ["review_mcq_questions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"]),
        sa.ForeignKeyConstraint(["session_item_id"], ["review_session_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_item_id", name="uq_review_mcq_questions_session_item_id"),
    )
    op.create_index("ix_review_mcq_questions_card", "review_mcq_questions", ["card_id", "created_at"])
    op.create_index("ix_review_mcq_questions_session", "review_mcq_questions", ["session_id", "created_at"])

    op.create_table(
        "review_answer_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("card_id", sa.Uuid(), nullable=True),
        sa.Column("source_card_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("session_item_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("client_action_id", sa.String(length=128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("is_repeat", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prompt_content_snapshot", sa.Text(), nullable=False),
        sa.Column("correct_answer_snapshot", sa.Text(), nullable=False),
        sa.Column("options_snapshot", json_type, nullable=False),
        sa.Column("option_order", json_type, nullable=False),
        sa.Column("selected_option_id", sa.String(length=64), nullable=False),
        sa.Column("selected_answer_text", sa.Text(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fsrs_rating", sa.String(length=16), nullable=False),
        sa.Column("fsrs_review_log_json", json_type, nullable=False),
        sa.Column("fsrs_state_before_json", json_type, nullable=False),
        sa.Column("fsrs_state_after_json", json_type, nullable=False),
        sa.Column("scheduler_name", sa.String(length=64), nullable=False, server_default="py-fsrs"),
        sa.Column("scheduler_version", sa.String(length=32), nullable=False, server_default="6.3.2"),
        sa.Column("scheduler_parameters", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("attempt_no IN (1, 2)", name="ck_review_answer_logs_attempt_no"),
        sa.CheckConstraint("fsrs_rating IN ('Again', 'Good')", name="ck_review_answer_logs_fsrs_rating"),
        sa.CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms >= 0",
            name="ck_review_answer_logs_response_time",
        ),
        sa.ForeignKeyConstraint(["card_id"], ["cards.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["question_id"], ["review_mcq_questions.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["review_sessions.id"]),
        sa.ForeignKeyConstraint(["session_item_id"], ["review_session_items.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_item_id", name="uq_review_answer_logs_session_item_id"),
        sa.UniqueConstraint("user_id", "client_action_id", name="uq_review_answer_logs_user_client_action"),
    )
    op.create_index("ix_review_answer_logs_card_answered_at", "review_answer_logs", ["card_id", "answered_at"])
    op.create_index(
        "ix_review_answer_logs_source_card_answered_at",
        "review_answer_logs",
        ["source_card_id", "answered_at"],
    )
    op.create_index("ix_review_answer_logs_session", "review_answer_logs", ["session_id", "answered_at"])
    op.create_index("ix_review_answer_logs_user_answered_at", "review_answer_logs", ["user_id", "answered_at"])


def downgrade() -> None:
    op.drop_index("ix_review_answer_logs_user_answered_at", table_name="review_answer_logs")
    op.drop_index("ix_review_answer_logs_session", table_name="review_answer_logs")
    op.drop_index("ix_review_answer_logs_source_card_answered_at", table_name="review_answer_logs")
    op.drop_index("ix_review_answer_logs_card_answered_at", table_name="review_answer_logs")
    op.drop_table("review_answer_logs")

    op.drop_index("ix_review_mcq_questions_session", table_name="review_mcq_questions")
    op.drop_index("ix_review_mcq_questions_card", table_name="review_mcq_questions")
    op.drop_table("review_mcq_questions")

    op.drop_index("ix_card_fsrs_states_user_due", table_name="card_fsrs_states")
    op.drop_index("ix_card_fsrs_states_card_due", table_name="card_fsrs_states")
    op.drop_table("card_fsrs_states")

    op.drop_index("ix_card_lexical_metadata_pos_bnc", table_name="card_lexical_metadata")
    op.drop_index("ix_card_lexical_metadata_pos_frq", table_name="card_lexical_metadata")
    op.drop_index("ix_card_lexical_metadata_content_normalized", table_name="card_lexical_metadata")
    op.drop_table("card_lexical_metadata")
