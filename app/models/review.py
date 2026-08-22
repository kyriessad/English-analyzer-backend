from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utc_now


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        CheckConstraint(
            "session_type IN ('daily_suggested', 'new_only', 'free_review')",
            name="ck_review_sessions_session_type",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'abandoned', 'expired')",
            name="ck_review_sessions_status",
        ),
        CheckConstraint("total_count >= 0", name="ck_review_sessions_total_count_nonnegative"),
        CheckConstraint(
            "completed_count >= 0",
            name="ck_review_sessions_completed_count_nonnegative",
        ),
        CheckConstraint(
            "reviewed_count >= 0",
            name="ck_review_sessions_reviewed_count_nonnegative",
        ),
        CheckConstraint(
            "current_index >= 0",
            name="ck_review_sessions_current_index_nonnegative",
        ),
        CheckConstraint(
            "planned_new_count >= 0",
            name="ck_review_sessions_planned_new_count_nonnegative",
        ),
        CheckConstraint(
            "planned_review_count >= 0",
            name="ck_review_sessions_planned_review_count_nonnegative",
        ),
        Index("ix_review_sessions_user_date_status", "user_id", "review_date", "status"),
        Index("ix_review_sessions_user_status", "user_id", "status"),
        Index(
            "ux_review_sessions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False, default="daily_suggested")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    planned_new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    planned_review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    user: Mapped["User"] = relationship(back_populates="review_sessions")
    items: Mapped[list["ReviewSessionItem"]] = relationship(back_populates="session")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="session")
    review_logs: Mapped[list["ReviewLog"]] = relationship(back_populates="session")


class ReviewSessionItem(Base):
    __tablename__ = "review_session_items"
    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_review_session_items_session_position"),
        CheckConstraint(
            "status IN ('pending', 'reviewed', 'done', 'skipped')",
            name="ck_review_session_items_status",
        ),
        CheckConstraint(
            "position >= 0",
            name="ck_review_session_items_position_nonnegative",
        ),
        CheckConstraint(
            "repeat_count >= 0",
            name="ck_review_session_items_repeat_count_nonnegative",
        ),
        CheckConstraint(
            "reappear_count >= 0",
            name="ck_review_session_items_reappear_count_nonnegative",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('forgot', 'shaky', 'got_it', 'fluent')",
            name="ck_review_session_items_result",
        ),
        CheckConstraint(
            "first_result IS NULL OR first_result IN ('forgot', 'shaky', 'got_it', 'fluent')",
            name="ck_review_session_items_first_result",
        ),
        CheckConstraint(
            "final_result IS NULL OR final_result IN ('forgot', 'shaky', 'got_it', 'fluent')",
            name="ck_review_session_items_final_result",
        ),
        Index("ix_review_session_items_session_status", "session_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_sessions.id"),
        nullable=False,
    )
    card_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cards.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reappear_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_repeat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    session: Mapped["ReviewSession"] = relationship(back_populates="items")
    card: Mapped["Card"] = relationship(back_populates="review_session_items")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="session_item")
    review_logs: Mapped[list["ReviewLog"]] = relationship(back_populates="session_item")


class ReviewRecord(Base):
    __tablename__ = "review_records"
    __table_args__ = (
        UniqueConstraint("user_id", "client_record_id", name="uq_review_records_user_client_record_id"),
        CheckConstraint("result IN ('again', 'hard', 'good', 'easy')", name="ck_review_records_result"),
        CheckConstraint("source IN ('miniapp', 'retry', 'migration')", name="ck_review_records_source"),
        Index("ix_review_records_user_review_date", "user_id", "review_date"),
        Index("ix_review_records_card_reviewed_at", "card_id", "reviewed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    card_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cards.id"), nullable=False)
    session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_sessions.id"),
        nullable=True,
    )
    session_item_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_session_items.id"),
        nullable=True,
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    result_label: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    before_next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    after_next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    before_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    after_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    client_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="miniapp")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="review_records")
    card: Mapped["Card"] = relationship(back_populates="review_records")
    session: Mapped["ReviewSession | None"] = relationship(back_populates="review_records")
    session_item: Mapped["ReviewSessionItem | None"] = relationship(back_populates="review_records")


json_messages_type = JSON().with_variant(JSONB, "postgresql")


class ReviewLog(Base):
    __tablename__ = "review_logs"
    __table_args__ = (
        UniqueConstraint("session_item_id", name="uq_review_logs_session_item_id"),
        CheckConstraint("result IN ('forgot', 'shaky', 'got_it', 'fluent')", name="ck_review_logs_result"),
        CheckConstraint(
            "session_type IN ('daily_suggested', 'new_only', 'free_review')",
            name="ck_review_logs_session_type",
        ),
        CheckConstraint(
            "card_state_before_review IN ('new', 'strengthening', 'reviewing', 'mastered')",
            name="ck_review_logs_card_state_before_review",
        ),
        CheckConstraint(
            "review_state_before IN ('new', 'strengthening', 'reviewing', 'mastered')",
            name="ck_review_logs_review_state_before",
        ),
        CheckConstraint(
            "review_state_after IN ('new', 'strengthening', 'reviewing', 'mastered')",
            name="ck_review_logs_review_state_after",
        ),
        CheckConstraint(
            "mastery_score_before BETWEEN 0 AND 5",
            name="ck_review_logs_mastery_score_before_range",
        ),
        CheckConstraint(
            "mastery_score_after BETWEEN 0 AND 5",
            name="ck_review_logs_mastery_score_after_range",
        ),
        CheckConstraint(
            "recovery_stage_before BETWEEN 0 AND 2",
            name="ck_review_logs_recovery_stage_before_range",
        ),
        CheckConstraint(
            "recovery_stage_after BETWEEN 0 AND 2",
            name="ck_review_logs_recovery_stage_after_range",
        ),
        Index("ix_review_logs_user_reviewed_at", "user_id", "reviewed_at"),
        Index("ix_review_logs_card_reviewed_at", "card_id", "reviewed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    card_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("cards.id"), nullable=False)
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("review_sessions.id"), nullable=False)
    session_item_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("review_session_items.id"),
        nullable=False,
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False, default="daily_suggested")
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    card_snapshot: Mapped[dict | None] = mapped_column(json_messages_type, nullable=True)
    card_state_before_review: Mapped[str] = mapped_column(String(32), nullable=False)
    review_state_before: Mapped[str] = mapped_column(String(32), nullable=False)
    review_state_after: Mapped[str] = mapped_column(String(32), nullable=False)
    mastery_score_before: Mapped[int] = mapped_column(Integer, nullable=False)
    mastery_score_after: Mapped[int] = mapped_column(Integer, nullable=False)
    recovery_stage_before: Mapped[int] = mapped_column(Integer, nullable=False)
    recovery_stage_after: Mapped[int] = mapped_column(Integer, nullable=False)
    next_review_at_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_review_at_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="review_logs")
    card: Mapped["Card"] = relationship(back_populates="review_logs")
    session: Mapped["ReviewSession"] = relationship(back_populates="review_logs")
    session_item: Mapped["ReviewSessionItem"] = relationship(back_populates="review_logs")


class ClientAction(Base):
    __tablename__ = "client_actions"
    __table_args__ = (
        UniqueConstraint("user_id", "client_action_id", name="ux_client_actions_user_action"),
        Index("ix_client_actions_created_at", "created_at"),
        Index("ix_client_actions_status_created_at", "status", "created_at"),
        CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed', 'ignored')",
            name="ck_client_actions_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    client_action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="client_actions")
