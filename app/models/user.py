from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wx_openid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    wx_unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    cards: Mapped[list["Card"]] = relationship(back_populates="user")
    review_sessions: Mapped[list["ReviewSession"]] = relationship(back_populates="user")
    review_records: Mapped[list["ReviewRecord"]] = relationship(back_populates="user")
    review_logs: Mapped[list["ReviewLog"]] = relationship(back_populates="user")
    client_actions: Mapped[list["ClientAction"]] = relationship(back_populates="user")
