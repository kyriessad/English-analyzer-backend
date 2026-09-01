from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utc_now


class PublicMaterialPack(Base):
    __tablename__ = "public_material_packs"
    __table_args__ = (
        CheckConstraint("kind IN ('word_book', 'expression', 'daily_quote')", name="ck_public_material_packs_kind"),
        CheckConstraint("status IN ('active', 'hidden')", name="ck_public_material_packs_status"),
        Index("ix_public_material_packs_status_sort", "status", "sort_order"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    content_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class PublicMaterialItem(Base):
    __tablename__ = "public_material_items"
    __table_args__ = (
        UniqueConstraint("pack_id", "content_normalized", name="uq_public_material_items_pack_content"),
        UniqueConstraint("pack_id", "position", name="uq_public_material_items_pack_position"),
        CheckConstraint("card_type IN ('word', 'phrase', 'sentence')", name="ck_public_material_items_card_type"),
        CheckConstraint("status IN ('approved', 'hidden')", name="ck_public_material_items_status"),
        CheckConstraint("position > 0", name="ck_public_material_items_position_positive"),
        Index("ix_public_material_items_pack_status_position", "pack_id", "status", "position"),
        Index("ix_public_material_items_content_normalized", "content_normalized"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    pack_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("public_material_packs.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    chinese: Mapped[str] = mapped_column(Text, nullable=False)
    card_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_label: Mapped[str] = mapped_column(String(120), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="approved")
    review_note: Mapped[str | None] = mapped_column(String(240), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class UserMaterialState(Base):
    __tablename__ = "user_material_states"
    __table_args__ = (
        UniqueConstraint("user_id", "material_item_id", name="uq_user_material_states_user_item"),
        CheckConstraint("state IN ('known')", name="ck_user_material_states_state"),
        Index("ix_user_material_states_user_state", "user_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    material_item_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("public_material_items.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="known")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
