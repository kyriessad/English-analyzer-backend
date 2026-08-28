from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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
        CheckConstraint(
            "review_state IN ('new', 'strengthening', 'reviewing', 'mastered')",
            name="ck_cards_review_state",
        ),
        CheckConstraint("status IN ('active', 'archived', 'deleted')", name="ck_cards_status"),
        Index("ix_cards_user_status", "user_id", "status"),
        Index("ix_cards_user_review_state", "user_id", "review_state"),
        Index("ix_cards_user_next_review_at", "user_id", "next_review_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
    where_encountered: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    example_sentence: Mapped[str | None] = mapped_column(Text, nullable=True)
    example_translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    is_review_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    needs_manual_fix: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    analysis_level: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_messages: Mapped[list[str]] = mapped_column(json_messages_type, nullable=False, default=list)
    understanding_source: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    review_state: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    mastery_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    again_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hard_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    good_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    easy_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forgot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shaky_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    got_it_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fluent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    __mapper_args__ = {"version_id_col": version}

    user: Mapped["User"] = relationship(back_populates="cards")
    review_session_items: Mapped[list["ReviewSessionItem"]] = relationship(back_populates="card")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="card")
    review_logs: Mapped[list["ReviewLog"]] = relationship(back_populates="card")
    lexical_metadata: Mapped["CardLexicalMetadata | None"] = relationship(
        back_populates="card",
        uselist=False,
        cascade="all, delete-orphan",
    )


class CardLexicalMetadata(Base):
    __tablename__ = "card_lexical_metadata"
    __table_args__ = (
        Index("ix_card_lexical_metadata_content_normalized", "content_normalized"),
        Index("ix_card_lexical_metadata_pos_frq", "pos", "frq"),
        Index("ix_card_lexical_metadata_pos_bnc", "pos", "bnc"),
    )

    card_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("cards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    content_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    edict_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pos: Mapped[str | None] = mapped_column(String(32), nullable=True)
    frq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bnc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    card: Mapped["Card"] = relationship(back_populates="lexical_metadata")
