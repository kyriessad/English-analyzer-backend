from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.user import utc_now


json_messages_type = JSON().with_variant(JSONB, "postgresql")


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("user_id", "legacy_cloud_id", name="uq_cards_user_legacy_cloud_id"),
        UniqueConstraint("user_id", "local_temp_id", name="uq_cards_user_local_temp_id"),
        CheckConstraint("card_type IN ('word', 'phrase', 'sentence')", name="ck_cards_card_type"),
        CheckConstraint("analysis_status IN ('pending', 'done', 'failed')", name="ck_cards_analysis_status"),
        CheckConstraint("analysis_level IN ('pass', 'warning', 'error')", name="ck_cards_analysis_level"),
        CheckConstraint(
            "understanding_source IN ('local', 'machine', 'ai', 'user')",
            name="ck_cards_understanding_source",
        ),
        CheckConstraint("status IN ('active', 'archived', 'deleted')", name="ck_cards_status"),
        Index("ix_cards_user_status", "user_id", "status"),
        Index("ix_cards_user_next_review_at", "user_id", "next_review_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    legacy_cloud_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    local_temp_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    card_type: Mapped[str] = mapped_column(String(32), nullable=False)
    exam_scene: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exam_module: Mapped[str | None] = mapped_column(String(64), nullable=True)
    understanding: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    analysis_level: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_messages: Mapped[list[str]] = mapped_column(json_messages_type, nullable=False, default=list)
    understanding_source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    again_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hard_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    good_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    easy_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_review_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="cards")
    review_session_items: Mapped[list["ReviewSessionItem"]] = relationship(back_populates="card")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="card")
