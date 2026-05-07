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
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utc_now


class ReviewSession(Base):
    __tablename__ = "review_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'completed', 'abandoned')", name="ck_review_sessions_status"),
        Index("ix_review_sessions_user_date_status", "user_id", "review_date", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    review_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="review_sessions")
    items: Mapped[list["ReviewSessionItem"]] = relationship(back_populates="session")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="session")


class ReviewSessionItem(Base):
    __tablename__ = "review_session_items"
    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_review_session_items_session_position"),
        CheckConstraint("status IN ('pending', 'done', 'skipped')", name="ck_review_session_items_status"),
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
    is_repeat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    final_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    session: Mapped["ReviewSession"] = relationship(back_populates="items")
    card: Mapped["Card"] = relationship(back_populates="review_session_items")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="session_item")


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
