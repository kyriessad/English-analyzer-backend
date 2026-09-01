from __future__ import annotations

from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.discovery import PublicMaterialItem, PublicMaterialPack, UserMaterialState
from app.models.user import User, utc_now
from app.schemas.discovery import MaterialItemResponse, MaterialPackResponse


DISCOVERY_TIMEZONE = "Asia/Shanghai"
DAILY_QUOTE_PACK_CODE = "daily-quote"


def discovery_local_date(now: datetime | None = None) -> date:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("UTC"))
    return current.astimezone(ZoneInfo(DISCOVERY_TIMEZONE)).date()


def _library_content_set(db: Session, user_id: UUID, normalized_contents: set[str]) -> set[str]:
    if not normalized_contents:
        return set()
    return set(db.scalars(select(Card.content_normalized).where(
        Card.user_id == user_id,
        Card.status == "active",
        Card.deleted_at.is_(None),
        Card.content_normalized.in_(normalized_contents),
    )))


def _known_item_set(db: Session, user_id: UUID, item_ids: set[UUID]) -> set[UUID]:
    if not item_ids:
        return set()
    return set(db.scalars(select(UserMaterialState.material_item_id).where(
        UserMaterialState.user_id == user_id,
        UserMaterialState.state == "known",
        UserMaterialState.material_item_id.in_(item_ids),
    )))


def item_response(item: PublicMaterialItem, pack: PublicMaterialPack, *, known: bool, in_library: bool) -> MaterialItemResponse:
    return MaterialItemResponse(
        id=item.id,
        pack_id=pack.id,
        pack_code=pack.code,
        pack_title=pack.title,
        content=item.content,
        chinese=item.chinese,
        card_type=item.card_type,
        source_label=item.source_label,
        known=known,
        in_library=in_library,
    )


def list_material_packs(db: Session, user: User) -> list[MaterialPackResponse]:
    packs = list(db.scalars(select(PublicMaterialPack).where(
        PublicMaterialPack.status == "active",
        PublicMaterialPack.kind != "daily_quote",
    ).order_by(PublicMaterialPack.sort_order, PublicMaterialPack.title)))
    responses: list[MaterialPackResponse] = []
    for pack in packs:
        approved_items = list(db.execute(select(
            PublicMaterialItem.id,
            PublicMaterialItem.content_normalized,
        ).where(
            PublicMaterialItem.pack_id == pack.id,
            PublicMaterialItem.status == "approved",
        )))
        known_ids = _known_item_set(db, user.id, {row.id for row in approved_items})
        library_contents = _library_content_set(db, user.id, {row.content_normalized for row in approved_items})
        remaining = sum(
            1 for row in approved_items
            if row.id not in known_ids and row.content_normalized not in library_contents
        )
        responses.append(MaterialPackResponse(
            id=pack.id,
            code=pack.code,
            title=pack.title,
            description=pack.description,
            kind=pack.kind,
            item_count=len(approved_items),
            remaining_count=remaining,
        ))
    return responses


def list_material_items(
    db: Session,
    user: User,
    *,
    pack_code: str,
    limit: int,
    offset: int,
    include_known: bool,
    query: str | None,
) -> tuple[list[MaterialItemResponse], int]:
    pack = db.scalar(select(PublicMaterialPack).where(
        PublicMaterialPack.code == pack_code,
        PublicMaterialPack.status == "active",
        PublicMaterialPack.kind != "daily_quote",
    ))
    if pack is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material pack not found")

    filters = [PublicMaterialItem.pack_id == pack.id, PublicMaterialItem.status == "approved"]
    cleaned_query = str(query or "").strip()
    if cleaned_query:
        filters.append(
            func.lower(PublicMaterialItem.content).like(f"%{cleaned_query.lower()}%")
            | PublicMaterialItem.chinese.like(f"%{cleaned_query}%")
        )
    if not include_known:
        filters.append(PublicMaterialItem.id.not_in(select(UserMaterialState.material_item_id).where(
            UserMaterialState.user_id == user.id,
            UserMaterialState.state == "known",
        )))

    total = int(db.scalar(select(func.count(PublicMaterialItem.id)).where(*filters)) or 0)
    items = list(db.scalars(select(PublicMaterialItem).where(*filters)
        .order_by(PublicMaterialItem.position).offset(offset).limit(limit)))
    known_ids = _known_item_set(db, user.id, {item.id for item in items})
    library_contents = _library_content_set(db, user.id, {item.content_normalized for item in items})
    return [
        item_response(
            item,
            pack,
            known=item.id in known_ids,
            in_library=item.content_normalized in library_contents,
        )
        for item in items
    ], total


def set_material_known(db: Session, user: User, item_id: UUID, known: bool) -> bool:
    item_exists = db.scalar(select(PublicMaterialItem.id).where(
        PublicMaterialItem.id == item_id,
        PublicMaterialItem.status == "approved",
    ))
    if item_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Material item not found")
    if not known:
        db.execute(delete(UserMaterialState).where(
            UserMaterialState.user_id == user.id,
            UserMaterialState.material_item_id == item_id,
        ))
        db.commit()
        return False

    existing = db.scalar(select(UserMaterialState).where(
        UserMaterialState.user_id == user.id,
        UserMaterialState.material_item_id == item_id,
    ))
    if existing is None:
        db.add(UserMaterialState(user_id=user.id, material_item_id=item_id, state="known"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.scalar(select(UserMaterialState.id).where(
                UserMaterialState.user_id == user.id,
                UserMaterialState.material_item_id == item_id,
            )) is None:
                raise
    return True


def get_today_quote(db: Session, user: User, *, today: date | None = None) -> tuple[date, MaterialItemResponse]:
    display_date = today or discovery_local_date()
    pack = db.scalar(select(PublicMaterialPack).where(
        PublicMaterialPack.code == DAILY_QUOTE_PACK_CODE,
        PublicMaterialPack.status == "active",
        PublicMaterialPack.kind == "daily_quote",
    ))
    if pack is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Today quote content is not ready")
    count = int(db.scalar(select(func.count(PublicMaterialItem.id)).where(
        PublicMaterialItem.pack_id == pack.id,
        PublicMaterialItem.status == "approved",
    )) or 0)
    if count < 1:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Today quote content is not ready")
    item = db.scalar(select(PublicMaterialItem).where(
        PublicMaterialItem.pack_id == pack.id,
        PublicMaterialItem.status == "approved",
    ).order_by(PublicMaterialItem.position).offset(display_date.toordinal() % count).limit(1))
    if item is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Today quote content is not ready")
    known = item.id in _known_item_set(db, user.id, {item.id})
    in_library = item.content_normalized in _library_content_set(db, user.id, {item.content_normalized})
    return display_date, item_response(item, pack, known=known, in_library=in_library)
