from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.user import User, utc_now
from app.schemas.card import CardCreate, CardResponse, CardUpdate


MAIN_REVIEW_STATES = ("new", "reviewing", "strengthening", "mastered")


def normalize_card_content(content: str) -> str:
    return " ".join(content.strip().lower().split())


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def recompute_card_readiness(card: Card) -> Card:
    card.is_review_ready = _has_text(card.content)
    card.needs_manual_fix = False
    return card


def get_card_or_404(
    db: Session,
    card_id: UUID,
    user_id: UUID | None = None,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Card:
    filters = [Card.id == card_id]
    if user_id is not None:
        filters.append(Card.user_id == user_id)

    statement = select(Card).where(*filters)
    if for_update:
        statement = statement.with_for_update()
    card = db.scalar(statement)
    if card is None or (card.deleted_at is not None and not include_deleted):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card not found")
    return card


def create_card_in_transaction(db: Session, payload: CardCreate) -> Card:
    if db.get(User, payload.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if payload.local_temp_id:
        existing_card = db.scalar(
            select(Card).where(
                Card.user_id == payload.user_id,
                Card.local_temp_id == payload.local_temp_id,
            )
        )
        if existing_card is not None:
            return existing_card

    now = utc_now()
    card = Card(
        user_id=payload.user_id,
        legacy_cloud_id=payload.legacy_cloud_id,
        local_temp_id=payload.local_temp_id,
        content=payload.content,
        content_normalized=normalize_card_content(payload.content),
        card_type=payload.card_type,
        exam_scene=payload.exam_scene,
        exam_module=payload.exam_module,
        understanding=payload.understanding,
        note=payload.note,
        where_encountered=payload.where_encountered,
        source_context=payload.source_context,
        source_url=payload.source_url,
        example_sentence=payload.example_sentence,
        example_translation=payload.example_translation,
        translation=payload.translation,
        analysis_status=payload.analysis_status,
        analysis_level=payload.analysis_level,
        analysis_messages=list(payload.analysis_messages),
        understanding_source=payload.understanding_source,
        review_count=0,
        again_count=0,
        hard_count=0,
        good_count=0,
        easy_count=0,
        next_review_at=payload.next_review_at or now,
        status="active",
    )
    recompute_card_readiness(card)
    db.add(card)
    db.flush()
    return card


def create_card(db: Session, payload: CardCreate) -> Card:
    try:
        card = create_card_in_transaction(db, payload)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if payload.local_temp_id:
            existing_card = db.scalar(
                select(Card).where(
                    Card.user_id == payload.user_id,
                    Card.local_temp_id == payload.local_temp_id,
                )
            )
            if existing_card is not None:
                return existing_card

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Card already exists for this user",
        ) from exc

    db.refresh(card)
    return card


def raise_card_version_conflict(card: Card) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "card_version_conflict",
            "message": "Card was changed on another device.",
            "server_card": CardResponse.model_validate(card).model_dump(mode="json"),
        },
    )


def apply_card_update(card: Card, payload: CardUpdate) -> Card:
    update_data = payload.model_dump(exclude_unset=True)
    expected_version = update_data.pop("base_version", None)
    if expected_version is not None and card.version != expected_version:
        raise_card_version_conflict(card)

    if update_data.get("content") is not None and update_data["content"] != card.content:
        card.content = update_data["content"]
        card.content_normalized = normalize_card_content(update_data["content"])
        card.analysis_status = "pending"

    for field in (
        "understanding",
        "translation",
        "note",
        "where_encountered",
        "source_context",
        "source_url",
        "example_sentence",
        "example_translation",
        "analysis_status",
        "analysis_level",
        "analysis_messages",
        "understanding_source",
        "card_type",
        "exam_scene",
        "exam_module",
    ):
        if field in update_data:
            setattr(card, field, update_data[field])

    recompute_card_readiness(card)
    return card


def list_cards(
    db: Session,
    *,
    user_id: UUID | None = None,
    keyword: str | None = None,
    limit: int = 20,
    offset: int = 0,
    status_filter: str = "active",
    review_state: str | None = None,
    include_deleted: bool = False,
    updated_since: datetime | None = None,
) -> tuple[list[Card], int]:
    # Incremental sync: return every card whose updated_at is after the cursor,
    # including soft-deleted tombstones (updated_at bumps on delete via status change).
    incremental = updated_since is not None

    filters = []
    if incremental:
        filters.append(Card.updated_at > updated_since)
    else:
        if not include_deleted:
            filters.append(Card.deleted_at.is_(None))
        if status_filter and not include_deleted:
            filters.append(Card.status == status_filter)
    if user_id is not None:
        filters.append(Card.user_id == user_id)
    if review_state and not incremental:
        filters.append(Card.review_state == review_state)

    keyword_normalized = normalize_card_content(keyword) if keyword else None
    if keyword_normalized:
        filters.append(Card.content_normalized.contains(keyword_normalized))

    total = db.scalar(select(func.count()).select_from(Card).where(*filters)) or 0

    order_by = (Card.updated_at.asc(), Card.id) if incremental else (Card.created_at.desc(), Card.id)
    cards = list(
        db.scalars(
            select(Card)
            .where(*filters)
            .order_by(*order_by)
            .offset(offset)
            .limit(limit)
        )
    )
    return cards, total


def update_card(db: Session, card_id: UUID, user_id: UUID, payload: CardUpdate) -> Card:
    card = get_card_or_404(db, card_id, user_id, for_update=True)
    apply_card_update(card, payload)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Card update conflicts with existing data",
        ) from exc

    db.refresh(card)
    return card


def get_cards_stats(db: Session, user_id: UUID) -> dict[str, int]:
    base_filters = [
        Card.user_id == user_id,
        Card.deleted_at.is_(None),
        Card.status != "deleted",
    ]

    state_counts = {
        review_state: db.scalar(
            select(func.count())
            .select_from(Card)
            .where(*base_filters, Card.review_state == review_state)
        ) or 0
        for review_state in MAIN_REVIEW_STATES
    }

    return {
        "total": sum(state_counts.values()),
        "new": state_counts["new"],
        "reviewing": state_counts["reviewing"],
        "strengthening": state_counts["strengthening"],
        "mastered": state_counts["mastered"],
        "needs_manual_fix": db.scalar(
            select(func.count())
            .select_from(Card)
            .where(*base_filters, Card.needs_manual_fix.is_(True))
        ) or 0,
        "pending": db.scalar(
            select(func.count())
            .select_from(Card)
            .where(*base_filters, Card.analysis_status == "pending")
        ) or 0,
    }


def soft_delete_card(card: Card, base_version: int | None = None) -> Card:
    if base_version is not None and card.version != base_version:
        raise_card_version_conflict(card)
    card.status = "deleted"
    card.deleted_at = utc_now()
    return card


def delete_card(
    db: Session,
    card_id: UUID,
    user_id: UUID,
    base_version: int | None = None,
) -> Card:
    card = get_card_or_404(db, card_id, user_id, for_update=True)
    soft_delete_card(card, base_version)
    db.commit()
    db.refresh(card)
    return card
