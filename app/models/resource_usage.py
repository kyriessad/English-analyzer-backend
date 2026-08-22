from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.user import utc_now


class ResourceUsage(Base):
    __tablename__ = "resource_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "resource", "usage_date", name="uq_resource_usage_user_day"),
        CheckConstraint("count >= 0", name="ck_resource_usage_count_nonnegative"),
        CheckConstraint(
            "resource IN ('ai', 'tts', 'lexical')",
            name="ck_resource_usage_resource",
        ),
        Index("ix_resource_usage_date_resource", "usage_date", "resource"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    resource: Mapped[str] = mapped_column(String(32), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
