from datetime import date, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.card import Card
from app.models.review import ReviewRecord, ReviewSession, ReviewSessionItem
from app.models.user import User, utc_now
from app.schemas.review import (
    CardReviewUpdateResponse,
    ReviewFeedbackRequest,
    ReviewFeedbackResponse,
    ReviewRecordResponse,
    ReviewSessionCardResponse,
    ReviewSessionItemResponse,
    ReviewSessionSummaryResponse,
    ReviewSessionUpdateResponse,
    ReviewSummaryCardItem,
    TodayReviewResponse,
)
from app.services.review_scheduler import (
    calculate_next_review_at,
    select_today_cards,
    should_append_repeat_item,
)


RESULT_LABELS = {
    "again": "again",
    "hard": "hard",
    "good": "good",
    "easy": "easy",
}


def _ensure_user_exists(db: Session, user_id: UUID) -> None:
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


def _get_session_or_404(db: Session, session_id: UUID, user_id: UUID | None = None) -> ReviewSession:
    filters = [ReviewSession.id == session_id]
    if user_id is not None:
        filters.append(ReviewSession.user_id == user_id)

    session = db.scalar(
        select(ReviewSession)
        .options(selectinload(ReviewSession.items).selectinload(ReviewSessionItem.card))
        .where(*filters)
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session not found")
    return session


def _get_item_or_404(db: Session, item_id: UUID, session_id: UUID) -> ReviewSessionItem:
    item = db.scalar(
        select(ReviewSessionItem)
        .options(selectinload(ReviewSessionItem.card))
        .where(
            ReviewSessionItem.id == item_id,
            ReviewSessionItem.session_id == session_id,
        )
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session item not found")
    return item


def _get_card_or_404(db: Session, card_id: UUID, user_id: UUID) -> Card:
    card = db.scalar(
        select(Card).where(
            Card.id == card_id,
            Card.user_id == user_id,
            Card.status == "active",
        )
    )
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card not found")
    return card


def _ordered_session_items(session: ReviewSession) -> list[ReviewSessionItem]:
    return sorted(session.items, key=lambda item: item.position)


def _item_response(item: ReviewSessionItem) -> ReviewSessionItemResponse:
    return ReviewSessionItemResponse(
        id=item.id,
        session_id=item.session_id,
        card_id=item.card_id,
        position=item.position,
        status=item.status,
        is_repeat=item.is_repeat,
        repeat_count=item.repeat_count,
        first_result=item.first_result,
        final_result=item.final_result,
        card=ReviewSessionCardResponse(
            id=item.card.id,
            content=item.card.content,
            card_type=item.card.card_type,
            review_count=item.card.review_count,
            last_review_result=item.card.last_review_result,
            next_review_at=item.card.next_review_at,
        ),
    )


def _today_response(session: ReviewSession) -> TodayReviewResponse:
    return TodayReviewResponse(
        session_id=session.id,
        review_date=session.review_date,
        timezone=session.timezone,
        status=session.status,
        total_count=session.total_count,
        completed_count=session.completed_count,
        current_index=session.current_index,
        items=[_item_response(item) for item in _ordered_session_items(session)],
    )


def _record_response(record: ReviewRecord) -> ReviewRecordResponse:
    return ReviewRecordResponse(
        id=record.id,
        user_id=record.user_id,
        card_id=record.card_id,
        session_id=record.session_id,
        session_item_id=record.session_item_id,
        result=record.result,
        result_label=record.result_label,
        reviewed_at=record.reviewed_at,
        review_date=record.review_date,
        before_next_review_at=record.before_next_review_at,
        after_next_review_at=record.after_next_review_at,
        before_review_count=record.before_review_count,
        after_review_count=record.after_review_count,
        client_record_id=record.client_record_id,
        source=record.source,
        created_at=record.created_at,
    )


def _card_update_response(card: Card) -> CardReviewUpdateResponse:
    return CardReviewUpdateResponse(
        id=card.id,
        review_count=card.review_count,
        again_count=card.again_count,
        hard_count=card.hard_count,
        good_count=card.good_count,
        easy_count=card.easy_count,
        last_review_result=card.last_review_result,
        last_reviewed_at=card.last_reviewed_at,
        next_review_at=card.next_review_at,
    )


def _session_update_response(session: ReviewSession) -> ReviewSessionUpdateResponse:
    return ReviewSessionUpdateResponse(
        session_id=session.id,
        status=session.status,
        total_count=session.total_count,
        completed_count=session.completed_count,
        current_index=session.current_index,
        completed_at=session.completed_at,
    )


def _next_pending_item(session: ReviewSession) -> ReviewSessionItem | None:
    for item in _ordered_session_items(session):
        if item.status == "pending":
            return item
    return None


def _refresh_session_progress(db: Session, session: ReviewSession) -> None:
    items = list(
        db.scalars(
            select(ReviewSessionItem)
            .where(ReviewSessionItem.session_id == session.id)
            .order_by(ReviewSessionItem.position)
        )
    )
    session.total_count = len(items)
    session.completed_count = sum(1 for item in items if item.status in {"done", "skipped"})

    next_pending = next((item for item in items if item.status == "pending"), None)
    session.current_index = next_pending.position if next_pending else session.total_count

    if items and next_pending is None:
        session.status = "completed"
        session.completed_at = session.completed_at or utc_now()


def get_or_create_today_session(
    db: Session,
    *,
    user_id: UUID,
    review_date: date,
    timezone: str,
) -> TodayReviewResponse:
    _ensure_user_exists(db, user_id)

    session = db.scalar(
        select(ReviewSession)
        .options(selectinload(ReviewSession.items).selectinload(ReviewSessionItem.card))
        .where(
            ReviewSession.user_id == user_id,
            ReviewSession.review_date == review_date,
            ReviewSession.status == "active",
        )
        .order_by(ReviewSession.created_at.desc())
    )
    if session is not None:
        return _today_response(session)

    cards = list(
        db.scalars(
            select(Card)
            .where(Card.user_id == user_id, Card.status == "active")
            .order_by(Card.created_at, Card.id)
        )
    )
    selected_cards = select_today_cards(cards, review_date, timezone)

    session = ReviewSession(
        user_id=user_id,
        review_date=review_date,
        timezone=timezone,
        status="active",
        total_count=len(selected_cards),
        completed_count=0,
        current_index=0,
    )
    db.add(session)
    db.flush()

    for position, card in enumerate(selected_cards):
        db.add(
            ReviewSessionItem(
                session_id=session.id,
                card_id=card.id,
                position=position,
                status="pending",
                is_repeat=False,
                repeat_count=0,
            )
        )

    db.commit()
    session = _get_session_or_404(db, session.id, user_id)
    return _today_response(session)


def submit_review_feedback(db: Session, payload: ReviewFeedbackRequest) -> ReviewFeedbackResponse:
    existing_record = db.scalar(
        select(ReviewRecord)
        .options(
            selectinload(ReviewRecord.card),
            selectinload(ReviewRecord.session).selectinload(ReviewSession.items).selectinload(ReviewSessionItem.card),
        )
        .where(
            ReviewRecord.user_id == payload.user_id,
            ReviewRecord.client_record_id == payload.client_record_id,
        )
    )
    if existing_record is not None:
        session = existing_record.session or _get_session_or_404(db, payload.session_id, payload.user_id)
        next_item = _next_pending_item(session)
        return ReviewFeedbackResponse(
            record=_record_response(existing_record),
            card_update=_card_update_response(existing_record.card),
            session_update=_session_update_response(session),
            next_item=_item_response(next_item) if next_item else None,
            completed=session.status == "completed",
            idempotent=True,
        )

    session = _get_session_or_404(db, payload.session_id, payload.user_id)
    item = _get_item_or_404(db, payload.session_item_id, session.id)
    card = _get_card_or_404(db, payload.card_id, payload.user_id)
    if item.card_id != card.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session item does not match card",
        )

    before_review_count = card.review_count
    before_next_review_at = card.next_review_at
    after_next_review_at = calculate_next_review_at(
        card,
        payload.result,
        payload.reviewed_at,
        payload.timezone,
    )

    card.review_count += 1
    if payload.result == "again":
        card.again_count += 1
    elif payload.result == "hard":
        card.hard_count += 1
    elif payload.result == "good":
        card.good_count += 1
    elif payload.result == "easy":
        card.easy_count += 1

    card.last_review_result = payload.result
    card.last_reviewed_at = payload.reviewed_at
    card.next_review_at = after_next_review_at

    item.status = "done"
    item.first_result = item.first_result or payload.result
    item.final_result = payload.result

    record = ReviewRecord(
        user_id=payload.user_id,
        card_id=card.id,
        session_id=session.id,
        session_item_id=item.id,
        result=payload.result,
        result_label=RESULT_LABELS[payload.result],
        reviewed_at=payload.reviewed_at,
        review_date=session.review_date,
        before_next_review_at=before_next_review_at,
        after_next_review_at=after_next_review_at,
        before_review_count=before_review_count,
        after_review_count=card.review_count,
        client_record_id=payload.client_record_id,
        source="miniapp",
    )
    db.add(record)

    if should_append_repeat_item(payload.result, item.repeat_count):
        max_position = db.scalar(
            select(func.max(ReviewSessionItem.position)).where(
                ReviewSessionItem.session_id == session.id
            )
        )
        db.add(
            ReviewSessionItem(
                session_id=session.id,
                card_id=card.id,
                position=(max_position if max_position is not None else -1) + 1,
                status="pending",
                is_repeat=True,
                repeat_count=item.repeat_count + 1,
                first_result=item.first_result,
            )
        )

    db.flush()
    _refresh_session_progress(db, session)
    db.commit()

    record = db.scalar(
        select(ReviewRecord)
        .options(selectinload(ReviewRecord.card))
        .where(ReviewRecord.id == record.id)
    )
    session = _get_session_or_404(db, session.id, payload.user_id)
    next_item = _next_pending_item(session)

    return ReviewFeedbackResponse(
        record=_record_response(record),
        card_update=_card_update_response(card),
        session_update=_session_update_response(session),
        next_item=_item_response(next_item) if next_item else None,
        completed=session.status == "completed",
        idempotent=False,
    )


def get_review_session_summary(db: Session, session_id: UUID) -> ReviewSessionSummaryResponse:
    session = _get_session_or_404(db, session_id)
    items = _ordered_session_items(session)

    return ReviewSessionSummaryResponse(
        session_id=session.id,
        review_date=session.review_date,
        total_count=session.total_count,
        completed_count=session.completed_count,
        again_count=sum(1 for item in items if item.final_result == "again"),
        hard_count=sum(1 for item in items if item.final_result == "hard"),
        good_count=sum(1 for item in items if item.final_result == "good"),
        easy_count=sum(1 for item in items if item.final_result == "easy"),
        items=[
            ReviewSummaryCardItem(
                card_id=item.card_id,
                content=item.card.content,
                final_result=item.final_result,
                review_count=item.card.review_count,
            )
            for item in items
        ],
    )
