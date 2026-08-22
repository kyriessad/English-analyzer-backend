"""add level 3.2 data integrity constraints

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-08-23 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_resource_usage_count_nonnegative",
        "resource_usage",
        '"count" >= 0',
    )
    op.create_check_constraint(
        "ck_resource_usage_resource",
        "resource_usage",
        "resource IN ('ai', 'tts', 'lexical')",
    )

    op.create_check_constraint(
        "ck_review_sessions_total_count_nonnegative",
        "review_sessions",
        "total_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_sessions_completed_count_nonnegative",
        "review_sessions",
        "completed_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_sessions_reviewed_count_nonnegative",
        "review_sessions",
        "reviewed_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_sessions_current_index_nonnegative",
        "review_sessions",
        "current_index >= 0",
    )
    op.create_check_constraint(
        "ck_review_sessions_planned_new_count_nonnegative",
        "review_sessions",
        "planned_new_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_sessions_planned_review_count_nonnegative",
        "review_sessions",
        "planned_review_count >= 0",
    )
    op.create_index(
        "ux_review_sessions_one_active_per_user",
        "review_sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_check_constraint(
        "ck_review_session_items_position_nonnegative",
        "review_session_items",
        "position >= 0",
    )
    op.create_check_constraint(
        "ck_review_session_items_repeat_count_nonnegative",
        "review_session_items",
        "repeat_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_session_items_reappear_count_nonnegative",
        "review_session_items",
        "reappear_count >= 0",
    )
    op.create_check_constraint(
        "ck_review_session_items_result",
        "review_session_items",
        "result IS NULL OR result IN ('forgot', 'shaky', 'got_it', 'fluent')",
    )
    op.create_check_constraint(
        "ck_review_session_items_first_result",
        "review_session_items",
        "first_result IS NULL OR first_result IN ('forgot', 'shaky', 'got_it', 'fluent')",
    )
    op.create_check_constraint(
        "ck_review_session_items_final_result",
        "review_session_items",
        "final_result IS NULL OR final_result IN ('forgot', 'shaky', 'got_it', 'fluent')",
    )

    op.create_unique_constraint(
        "uq_review_logs_session_item_id",
        "review_logs",
        ["session_item_id"],
    )
    op.create_check_constraint(
        "ck_review_logs_session_type",
        "review_logs",
        "session_type IN ('daily_suggested', 'new_only', 'free_review')",
    )
    op.create_check_constraint(
        "ck_review_logs_card_state_before_review",
        "review_logs",
        "card_state_before_review IN ('new', 'strengthening', 'reviewing', 'mastered')",
    )
    op.create_check_constraint(
        "ck_review_logs_review_state_before",
        "review_logs",
        "review_state_before IN ('new', 'strengthening', 'reviewing', 'mastered')",
    )
    op.create_check_constraint(
        "ck_review_logs_review_state_after",
        "review_logs",
        "review_state_after IN ('new', 'strengthening', 'reviewing', 'mastered')",
    )
    op.create_check_constraint(
        "ck_review_logs_mastery_score_before_range",
        "review_logs",
        "mastery_score_before BETWEEN 0 AND 5",
    )
    op.create_check_constraint(
        "ck_review_logs_mastery_score_after_range",
        "review_logs",
        "mastery_score_after BETWEEN 0 AND 5",
    )
    op.create_check_constraint(
        "ck_review_logs_recovery_stage_before_range",
        "review_logs",
        "recovery_stage_before BETWEEN 0 AND 2",
    )
    op.create_check_constraint(
        "ck_review_logs_recovery_stage_after_range",
        "review_logs",
        "recovery_stage_after BETWEEN 0 AND 2",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_review_logs_recovery_stage_after_range",
        "review_logs",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_logs_recovery_stage_before_range",
        "review_logs",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_logs_mastery_score_after_range",
        "review_logs",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_logs_mastery_score_before_range",
        "review_logs",
        type_="check",
    )
    op.drop_constraint("ck_review_logs_review_state_after", "review_logs", type_="check")
    op.drop_constraint("ck_review_logs_review_state_before", "review_logs", type_="check")
    op.drop_constraint(
        "ck_review_logs_card_state_before_review",
        "review_logs",
        type_="check",
    )
    op.drop_constraint("ck_review_logs_session_type", "review_logs", type_="check")
    op.drop_constraint("uq_review_logs_session_item_id", "review_logs", type_="unique")

    op.drop_constraint(
        "ck_review_session_items_final_result",
        "review_session_items",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_session_items_first_result",
        "review_session_items",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_session_items_result",
        "review_session_items",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_session_items_reappear_count_nonnegative",
        "review_session_items",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_session_items_repeat_count_nonnegative",
        "review_session_items",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_session_items_position_nonnegative",
        "review_session_items",
        type_="check",
    )

    op.drop_index("ux_review_sessions_one_active_per_user", table_name="review_sessions")
    op.drop_constraint(
        "ck_review_sessions_planned_review_count_nonnegative",
        "review_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_sessions_planned_new_count_nonnegative",
        "review_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_sessions_current_index_nonnegative",
        "review_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_sessions_reviewed_count_nonnegative",
        "review_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_sessions_completed_count_nonnegative",
        "review_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_review_sessions_total_count_nonnegative",
        "review_sessions",
        type_="check",
    )

    op.drop_constraint("ck_resource_usage_resource", "resource_usage", type_="check")
    op.drop_constraint(
        "ck_resource_usage_count_nonnegative",
        "resource_usage",
        type_="check",
    )
