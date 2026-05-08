"""phase4a review contract fields

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _check_constraints(table_name: str) -> dict[str, str]:
    checks: dict[str, str] = {}
    for constraint in inspect(op.get_bind()).get_check_constraints(table_name):
        name = constraint.get("name")
        sqltext = constraint.get("sqltext") or ""
        if name:
            checks[name] = str(sqltext)
    return checks


def upgrade() -> None:
    card_columns = _columns("cards")
    with op.batch_alter_table("cards") as batch_op:
        if "is_review_ready" not in card_columns:
            batch_op.add_column(
                sa.Column("is_review_ready", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        if "needs_manual_fix" not in card_columns:
            batch_op.add_column(
                sa.Column("needs_manual_fix", sa.Boolean(), nullable=False, server_default=sa.false())
            )

    op.execute(
        """
        UPDATE cards
        SET is_review_ready = CASE
            WHEN TRIM(COALESCE(content, '')) <> ''
             AND (
                TRIM(COALESCE(understanding, '')) <> ''
                OR TRIM(COALESCE(translation, '')) <> ''
             )
            THEN TRUE ELSE FALSE
        END
        """
    )
    op.execute(
        """
        UPDATE cards
        SET needs_manual_fix = CASE
            WHEN analysis_status = 'failed' AND is_review_ready = FALSE
            THEN TRUE ELSE FALSE
        END
        """
    )

    session_columns = _columns("review_sessions")
    session_checks = _check_constraints("review_sessions")
    status_check = session_checks.get("ck_review_sessions_status", "")
    has_new_status_check = "expired" in status_check
    has_session_type_check = "ck_review_sessions_session_type" in session_checks

    with op.batch_alter_table("review_sessions") as batch_op:
        if "session_type" not in session_columns:
            batch_op.add_column(
                sa.Column(
                    "session_type",
                    sa.String(length=32),
                    nullable=False,
                    server_default="daily_suggested",
                )
            )
        if "planned_new_count" not in session_columns:
            batch_op.add_column(
                sa.Column("planned_new_count", sa.Integer(), nullable=False, server_default="0")
            )
        if "planned_review_count" not in session_columns:
            batch_op.add_column(
                sa.Column("planned_review_count", sa.Integer(), nullable=False, server_default="0")
            )
        if status_check and not has_new_status_check:
            batch_op.drop_constraint("ck_review_sessions_status", type_="check")
        if not has_new_status_check:
            batch_op.create_check_constraint(
                "ck_review_sessions_status",
                "status IN ('active', 'completed', 'abandoned', 'expired')",
            )
        if not has_session_type_check:
            batch_op.create_check_constraint(
                "ck_review_sessions_session_type",
                "session_type IN ('daily_suggested', 'new_only', 'free_review')",
            )

    op.execute(
        """
        UPDATE review_sessions
        SET planned_new_count = (
            SELECT COUNT(*)
            FROM review_session_items
            JOIN cards ON cards.id = review_session_items.card_id
            WHERE review_session_items.session_id = review_sessions.id
              AND cards.review_state = 'new'
        )
        """
    )
    op.execute(
        """
        UPDATE review_sessions
        SET planned_review_count = CASE
            WHEN total_count - planned_new_count < 0 THEN 0
            ELSE total_count - planned_new_count
        END
        """
    )

    log_columns = _columns("review_logs")
    with op.batch_alter_table("review_logs") as batch_op:
        if "session_type" not in log_columns:
            batch_op.add_column(
                sa.Column(
                    "session_type",
                    sa.String(length=32),
                    nullable=False,
                    server_default="daily_suggested",
                )
            )
        if "card_state_before_review" not in log_columns:
            batch_op.add_column(
                sa.Column(
                    "card_state_before_review",
                    sa.String(length=32),
                    nullable=False,
                    server_default="new",
                )
            )

    op.execute(
        """
        UPDATE review_logs
        SET card_state_before_review = review_state_before
        """
    )
    op.execute(
        """
        UPDATE review_logs
        SET session_type = COALESCE(
            (
                SELECT review_sessions.session_type
                FROM review_sessions
                WHERE review_sessions.id = review_logs.session_id
            ),
            'daily_suggested'
        )
        """
    )


def downgrade() -> None:
    log_columns = _columns("review_logs")
    with op.batch_alter_table("review_logs") as batch_op:
        if "card_state_before_review" in log_columns:
            batch_op.drop_column("card_state_before_review")
        if "session_type" in log_columns:
            batch_op.drop_column("session_type")

    session_columns = _columns("review_sessions")
    session_checks = _check_constraints("review_sessions")
    with op.batch_alter_table("review_sessions") as batch_op:
        if "ck_review_sessions_session_type" in session_checks:
            batch_op.drop_constraint("ck_review_sessions_session_type", type_="check")
        if "ck_review_sessions_status" in session_checks:
            batch_op.drop_constraint("ck_review_sessions_status", type_="check")
        batch_op.create_check_constraint(
            "ck_review_sessions_status",
            "status IN ('active', 'completed', 'abandoned')",
        )
        if "planned_review_count" in session_columns:
            batch_op.drop_column("planned_review_count")
        if "planned_new_count" in session_columns:
            batch_op.drop_column("planned_new_count")
        if "session_type" in session_columns:
            batch_op.drop_column("session_type")

    card_columns = _columns("cards")
    with op.batch_alter_table("cards") as batch_op:
        if "needs_manual_fix" in card_columns:
            batch_op.drop_column("needs_manual_fix")
        if "is_review_ready" in card_columns:
            batch_op.drop_column("is_review_ready")
