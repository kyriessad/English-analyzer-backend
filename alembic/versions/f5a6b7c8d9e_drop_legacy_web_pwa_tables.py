"""drop legacy web pwa tables

Revision ID: f5a6b7c8d9e
Revises: e4f5a6b7c8d9
Create Date: 2026-08-23 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These tables have no foreign-key dependencies on one another. Do not use
    # CASCADE: an unexpected external dependency must stop this migration.
    op.drop_table("admin_audit_logs")
    op.drop_table("feedback")
    op.drop_table("email_action_tokens")
    op.drop_table("web_sessions")


def downgrade() -> None:
    # Recreate the exact definitions from b1c2d3e4f5a6_phase1_web_accounts.py.
    op.create_table(
        "web_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=128), nullable=True),
        sa.Column("device_label", sa.String(length=128), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_digest", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_token_hash"),
    )
    op.create_index(
        "ix_web_sessions_user_active",
        "web_sessions",
        ["user_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_web_sessions_session_token_hash",
        "web_sessions",
        ["session_token_hash"],
        unique=True,
    )

    op.create_table(
        "email_action_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip_digest", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name="ck_email_action_tokens_purpose",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_email_action_tokens_token_hash",
        "email_action_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_email_action_tokens_user_purpose",
        "email_action_tokens",
        ["user_id", "purpose", "used_at"],
    )

    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_source", sa.String(length=200), nullable=True),
        sa.Column("error_info", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "feedback_type IN ('bug', 'feature', 'content', 'other')",
            name="ck_feedback_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'reviewing', 'closed')",
            name="ck_feedback_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_status_created_at", "feedback", ["status", "created_at"])

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("event_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_logs_actor_created_at",
        "admin_audit_logs",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_admin_audit_logs_target_created_at",
        "admin_audit_logs",
        ["target_user_id", "created_at"],
    )
