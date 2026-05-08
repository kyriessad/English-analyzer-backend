from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.card import Card
from app.models.review import ReviewLog, ReviewSession, ReviewSessionItem
from app.models.user import User, utc_now
from app.schemas.reviews import (
    ReviewFeedbackRequest,
    ReviewFeedbackResponse,
    ReviewItemResponse,
    ReviewOverviewResponse,
    ReviewProgressResponse,
    ReviewSessionCreateRequest,
    ReviewSessionCreateResponse,
    ReviewSummaryResponse,
    SessionSummaryResponse,
    TodayReviewsResponse,
)
from app.services.auth_service import get_current_user
from app.services.idempotency import (
    create_processing_action,
    get_existing_client_action,
    mark_action_failed,
    mark_action_ignored,
    mark_action_succeeded,
)
from app.services.review_rules import (
    apply_review_feedback_to_card,
    calculate_effective_new_quota,
    calculate_reappear_insert_position,
    get_due_reason,
    normalize_review_limit,
    select_review_cards,
    should_append_reappear_item,
)


router = APIRouter(prefix="/api/reviews", tags=["reviews"])
review_sessions_router = APIRouter(prefix="/api/review-sessions", tags=["review-sessions"])


MAIN_REVIEW_STATES = ("new", "reviewing", "strengthening", "mastered")


def _active_card_filters(user_id: UUID) -> list:
    return [
        Card.user_id == user_id,
        Card.deleted_at.is_(None),
        Card.status == "active",
    ]


def _review_ready_card_filters(user_id: UUID) -> list:
    return [
        *_active_card_filters(user_id),
        Card.review_state.in_(MAIN_REVIEW_STATES),
        Card.is_review_ready.is_(True),
        Card.analysis_status != "pending",
        Card.needs_manual_fix.is_(False),
    ]


def _local_review_date(now: datetime, timezone_name: str) -> date:
    try:
        return now.astimezone(ZoneInfo(timezone_name or "UTC")).date()
    except Exception:
        return now.date()


def _item_response(item: ReviewSessionItem, now: datetime) -> ReviewItemResponse:
    card = item.card
    return ReviewItemResponse(
        session_item_id=item.id,
        card_id=item.card_id,
        content=card.content,
        understanding=card.understanding or "",
        note=card.note or "",
        card_type=card.card_type,
        review_state=card.review_state,
        mastery_score=card.mastery_score,
        recovery_stage=card.recovery_stage,
        due_reason=get_due_reason(card, now) or "pending",
    )


def _pending_items(db: Session, session_id: UUID) -> list[ReviewSessionItem]:
    return list(
        db.scalars(
            select(ReviewSessionItem)
            .options(selectinload(ReviewSessionItem.card))
            .where(
                ReviewSessionItem.session_id == session_id,
                ReviewSessionItem.status == "pending",
            )
            .order_by(ReviewSessionItem.position, ReviewSessionItem.created_at)
        )
    )


def _next_pending_item(db: Session, session_id: UUID) -> ReviewSessionItem | None:
    return db.scalar(
        select(ReviewSessionItem)
        .options(selectinload(ReviewSessionItem.card))
        .where(
            ReviewSessionItem.session_id == session_id,
            ReviewSessionItem.status == "pending",
        )
        .order_by(ReviewSessionItem.position, ReviewSessionItem.created_at)
        .limit(1)
    )


def _progress_response(session: ReviewSession) -> ReviewProgressResponse:
    return ReviewProgressResponse(
        reviewed=session.reviewed_count,
        total=session.total_count,
    )


def _refresh_session_progress(db: Session, session: ReviewSession, now: datetime) -> None:
    total_count = db.scalar(
        select(func.count()).select_from(ReviewSessionItem).where(ReviewSessionItem.session_id == session.id)
    ) or 0
    reviewed_count = db.scalar(
        select(func.count())
        .select_from(ReviewSessionItem)
        .where(
            ReviewSessionItem.session_id == session.id,
            ReviewSessionItem.status.in_(("reviewed", "done", "skipped")),
        )
    ) or 0
    next_pending = _next_pending_item(db, session.id)

    session.total_count = total_count
    session.reviewed_count = reviewed_count
    session.completed_count = reviewed_count
    session.current_index = next_pending.position if next_pending is not None else total_count

    if total_count <= 0:
        session.status = "abandoned"
    elif next_pending is None:
        session.status = "completed"
        session.completed_at = session.completed_at or now


def _today_response(session: ReviewSession | None, limit: int, now: datetime, items: list[ReviewSessionItem]) -> TodayReviewsResponse:
    if session is None:
        return TodayReviewsResponse(
            session_id=None,
            limit=limit,
            progress=ReviewProgressResponse(reviewed=0, total=0),
            items=[],
        )

    return TodayReviewsResponse(
        session_id=session.id,
        limit=limit,
        progress=_progress_response(session),
        items=[_item_response(item, now) for item in items],
    )


def _active_sessions(db: Session, user_id: UUID) -> list[ReviewSession]:
    return list(
        db.scalars(
            select(ReviewSession)
            .where(
                ReviewSession.user_id == user_id,
                ReviewSession.status == "active",
            )
            .order_by(ReviewSession.started_at.desc(), ReviewSession.created_at.desc())
        )
    )


def _get_active_session(
    db: Session,
    user_id: UUID,
    session_type: str | None = None,
) -> ReviewSession | None:
    filters = [
        ReviewSession.user_id == user_id,
        ReviewSession.status == "active",
    ]
    if session_type is not None:
        filters.append(ReviewSession.session_type == session_type)

    return db.scalar(
        select(ReviewSession)
        .where(*filters)
        .order_by(ReviewSession.started_at.desc(), ReviewSession.created_at.desc())
        .limit(1)
    )


def _create_session(
    db: Session,
    *,
    user: User,
    cards: list[Card],
    limit: int,
    now: datetime,
    session_type: str = "daily_suggested",
) -> ReviewSession:
    planned_new_count = sum(1 for card in cards if card.review_state == "new")
    planned_review_count = len(cards) - planned_new_count
    session = ReviewSession(
        user_id=user.id,
        review_date=_local_review_date(now, user.timezone),
        timezone=user.timezone,
        session_type=session_type,
        started_at=now,
        status="active",
        batch_size=limit,
        total_count=len(cards),
        reviewed_count=0,
        completed_count=0,
        planned_new_count=planned_new_count,
        planned_review_count=planned_review_count,
        current_index=0,
    )
    db.add(session)
    db.flush()

    for position, card in enumerate(cards):
        db.add(
            ReviewSessionItem(
                session_id=session.id,
                card_id=card.id,
                position=position,
                status="pending",
                reappear_count=0,
                is_repeat=False,
                repeat_count=0,
            )
        )

    db.flush()
    return session


def _build_summary(db: Session, session_id: UUID) -> ReviewSummaryResponse:
    reviewed_items = list(
        db.scalars(
            select(ReviewSessionItem)
            .options(selectinload(ReviewSessionItem.card))
            .where(
                ReviewSessionItem.session_id == session_id,
                ReviewSessionItem.status == "reviewed",
            )
            .order_by(ReviewSessionItem.position)
        )
    )
    counts = {
        "forgot": 0,
        "shaky": 0,
        "got_it": 0,
        "fluent": 0,
    }
    unique_card_ids = set()
    strengthening_card_ids = set()
    mastered_card_ids = set()

    for item in reviewed_items:
        unique_card_ids.add(item.card_id)
        if item.result in counts:
            counts[item.result] += 1
        if item.card.review_state == "strengthening":
            strengthening_card_ids.add(item.card_id)
        if item.card.review_state == "mastered":
            mastered_card_ids.add(item.card_id)

    return ReviewSummaryResponse(
        unique_card_count=len(unique_card_ids),
        total_review_count=len(reviewed_items),
        forgot=counts["forgot"],
        shaky=counts["shaky"],
        got_it=counts["got_it"],
        fluent=counts["fluent"],
        strengthening_count=len(strengthening_card_ids),
        mastered_count=len(mastered_card_ids),
    )


def _shift_items_for_insert(db: Session, session_id: UUID, target_position: int, current_max_position: int) -> None:
    if target_position > current_max_position:
        return

    offset = current_max_position + 1000
    db.execute(
        update(ReviewSessionItem)
        .where(
            ReviewSessionItem.session_id == session_id,
            ReviewSessionItem.position >= target_position,
        )
        .values(position=ReviewSessionItem.position + offset)
    )
    db.execute(
        update(ReviewSessionItem)
        .where(
            ReviewSessionItem.session_id == session_id,
            ReviewSessionItem.position >= target_position + offset,
        )
        .values(position=ReviewSessionItem.position - (offset - 1))
    )


def _select_new_only_cards(user_id: UUID, limit: int, db: Session) -> list[Card]:
    return list(
        db.scalars(
            select(Card)
            .where(
                *_review_ready_card_filters(user_id),
                Card.review_state == "new",
            )
            .order_by(Card.created_at, Card.id)
            .limit(limit)
        )
    )


def _select_free_review_cards(user_id: UUID, limit: int, now: datetime, db: Session) -> list[Card]:
    return list(
        db.scalars(
            select(Card)
            .where(
                *_review_ready_card_filters(user_id),
                Card.review_state != "new",
                or_(
                    Card.review_state == "strengthening",
                    Card.next_review_at <= now,
                ),
            )
            .order_by(Card.next_review_at.is_(None), Card.next_review_at, Card.created_at, Card.id)
            .limit(limit)
        )
    )


def _select_session_cards(
    *,
    user_id: UUID,
    session_type: str,
    limit: int,
    now: datetime,
    db: Session,
) -> list[Card]:
    if session_type == "new_only":
        return _select_new_only_cards(user_id, limit, db)

    if session_type == "free_review":
        return _select_free_review_cards(user_id, limit, now, db)

    return select_review_cards(user_id, limit, now, db)


def _create_or_return_session(
    db: Session,
    *,
    user: User,
    session_type: str,
    limit: int,
    restart: bool,
    now: datetime,
) -> tuple[ReviewSession | None, list[ReviewSessionItem]]:
    normalized_limit = normalize_review_limit(limit)

    active_sessions = _active_sessions(db, user.id)
    for session in active_sessions:
        _refresh_session_progress(db, session, now)
    db.flush()
    active_sessions = [session for session in active_sessions if session.status == "active"]

    active_same = next((session for session in active_sessions if session.session_type == session_type), None)
    if active_same is not None and not restart:
        return active_same, _pending_items(db, active_same.id)

    active_other = next((session for session in active_sessions if session.session_type != session_type), None)
    if active_other is not None and not restart:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Different active review session exists. Pass restart=true to abandon it.",
        )

    if restart:
        for session in active_sessions:
            session.status = "abandoned"
        db.flush()

    selected_cards = _select_session_cards(
        user_id=user.id,
        session_type=session_type,
        limit=normalized_limit,
        now=now,
        db=db,
    )
    if not selected_cards:
        return None, []

    session = _create_session(
        db,
        user=user,
        cards=selected_cards,
        limit=normalized_limit,
        now=now,
        session_type=session_type,
    )
    db.flush()
    return session, _pending_items(db, session.id)


def _session_create_response(
    session: ReviewSession | None,
    *,
    session_type: str,
    limit: int,
    now: datetime,
    items: list[ReviewSessionItem],
) -> ReviewSessionCreateResponse:
    if session is None:
        return ReviewSessionCreateResponse(
            session_id=None,
            session_type=session_type,
            status=None,
            limit=limit,
            progress=ReviewProgressResponse(reviewed=0, total=0),
            items=[],
        )

    return ReviewSessionCreateResponse(
        session_id=session.id,
        session_type=session.session_type,
        status=session.status,
        limit=limit,
        planned_new_count=session.planned_new_count,
        planned_review_count=session.planned_review_count,
        progress=_progress_response(session),
        items=[_item_response(item, now) for item in items],
    )


@router.get("/overview", response_model=ReviewOverviewResponse)
def get_review_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewOverviewResponse:
    now = utc_now()
    base_filters = _review_ready_card_filters(current_user.id)
    today = _local_review_date(now, current_user.timezone)

    strengthening_count = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(*base_filters, Card.review_state == "strengthening")
    ) or 0
    due_count = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            *base_filters,
            Card.review_state.in_(("reviewing", "mastered")),
            Card.next_review_at <= now,
        )
    ) or 0
    new_available_count = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(*base_filters, Card.review_state == "new")
    ) or 0
    suggested_new_count = min(
        calculate_effective_new_quota(5, strengthening_count, due_count),
        new_available_count,
    )
    suggested_review_count = strengthening_count + due_count

    daily_session = db.scalar(
        select(ReviewSession)
        .where(
            ReviewSession.user_id == current_user.id,
            ReviewSession.review_date == today,
            ReviewSession.session_type == "daily_suggested",
            ReviewSession.status.in_(("active", "completed")),
        )
        .order_by(ReviewSession.started_at.desc(), ReviewSession.created_at.desc())
        .limit(1)
    )
    if daily_session is not None and daily_session.total_count > 0:
        suggested_new_count = daily_session.planned_new_count
        suggested_review_count = daily_session.planned_review_count

    completed_new_count = db.scalar(
        select(func.count())
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "daily_suggested",
            ReviewSession.review_date == today,
            ReviewLog.card_state_before_review == "new",
        )
    ) or 0
    completed_review_count = db.scalar(
        select(func.count())
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "daily_suggested",
            ReviewSession.review_date == today,
            ReviewLog.card_state_before_review != "new",
        )
    ) or 0
    new_only_count = db.scalar(
        select(func.count())
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "new_only",
            ReviewSession.review_date == today,
        )
    ) or 0
    free_review_count = db.scalar(
        select(func.count())
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "free_review",
            ReviewSession.review_date == today,
        )
    ) or 0

    active_session = _get_active_session(db, current_user.id)
    active_session_response = None
    if active_session is not None:
        _refresh_session_progress(db, active_session, now)
        remaining_count = db.scalar(
            select(func.count())
            .select_from(ReviewSessionItem)
            .where(
                ReviewSessionItem.session_id == active_session.id,
                ReviewSessionItem.status == "pending",
            )
        ) or 0
        if (
            active_session.status == "active"
            and remaining_count > 0
            and active_session.total_count > 0
        ):
            active_session_response = {
                "id": active_session.id,
                "session_type": active_session.session_type,
                "remaining_count": remaining_count,
                "total_count": active_session.total_count,
                "reviewed_count": active_session.reviewed_count,
                "status": active_session.status,
            }

    suggested_total_count = suggested_review_count + suggested_new_count
    completed_suggested_total = completed_review_count + completed_new_count
    is_all_done = (
        suggested_total_count == 0
        or (daily_session is not None and daily_session.status == "completed")
        or (
            active_session_response is None
            and suggested_total_count > 0
            and completed_suggested_total >= suggested_total_count
        )
    )

    db.commit()

    return ReviewOverviewResponse(
        suggested={
            "review_count": suggested_review_count,
            "new_count": suggested_new_count,
            "strengthening_count": strengthening_count,
            "due_count": due_count,
            "total_count": suggested_total_count,
        },
        completed_suggested={
            "review_count": completed_review_count,
            "new_count": completed_new_count,
            "total_count": completed_suggested_total,
        },
        extra_today={
            "new_only_count": new_only_count,
            "free_review_count": free_review_count,
            "total_count": new_only_count + free_review_count,
        },
        is_all_done=is_all_done,
        active_session=active_session_response,
    )


@router.get("/today", response_model=TodayReviewsResponse)
def get_today_reviews(
    limit: int = Query(default=5),
    restart: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TodayReviewsResponse:
    normalized_limit = normalize_review_limit(limit)
    now = utc_now()

    session, pending_items = _create_or_return_session(
        db,
        user=current_user,
        session_type="daily_suggested",
        now=now,
        limit=normalized_limit,
        restart=restart,
    )
    db.commit()
    if session is None:
        return _today_response(None, normalized_limit, now, [])
    db.refresh(session)
    return _today_response(session, normalized_limit, now, pending_items)


@review_sessions_router.post("", response_model=ReviewSessionCreateResponse)
def create_review_session_endpoint(
    payload: ReviewSessionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewSessionCreateResponse:
    normalized_limit = normalize_review_limit(payload.limit)
    now = utc_now()
    session, pending_items = _create_or_return_session(
        db,
        user=current_user,
        session_type=payload.session_type,
        limit=normalized_limit,
        restart=payload.restart,
        now=now,
    )
    db.commit()
    if session is not None:
        db.refresh(session)
    return _session_create_response(
        session,
        session_type=payload.session_type,
        limit=normalized_limit,
        now=now,
        items=pending_items,
    )


@router.post("/feedback", response_model=ReviewFeedbackResponse)
def submit_review_feedback(
    payload: ReviewFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewFeedbackResponse:
    now = utc_now()

    # Step 1: Check client_action_id idempotency
    existing_action = get_existing_client_action(db, current_user.id, payload.client_action_id)
    if existing_action is not None:
        if existing_action.status == "succeeded":
            # Return the saved response_payload from the first successful processing
            saved = existing_action.response_payload or {}
            return ReviewFeedbackResponse(
                done=saved.get("done", False),
                next_item=None,
                summary=None,
                progress=ReviewProgressResponse(
                    reviewed=saved.get("progress", {}).get("reviewed", 0),
                    total=saved.get("progress", {}).get("total", 0),
                ),
                status="success",
            )
        if existing_action.status == "ignored":
            saved = existing_action.response_payload or {}
            return ReviewFeedbackResponse(
                done=False,
                next_item=None,
                summary=None,
                progress=ReviewProgressResponse(
                    reviewed=saved.get("progress", {}).get("reviewed", 0),
                    total=saved.get("progress", {}).get("total", 0),
                ),
                status="ignored",
                ignored_reason=existing_action.error_message or "action_ignored",
            )
        if existing_action.status == "processing":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Action is being processed, please retry later.",
            )

    # Step 2: Create processing record (within transaction)
    try:
        action = create_processing_action(
            db,
            current_user.id,
            payload.client_action_id,
            "review_feedback",
            request_payload=payload.model_dump(mode="json"),
        )

        # Step 3: Lock and validate session
        session = db.scalar(
            select(ReviewSession)
            .where(
                ReviewSession.id == payload.session_id,
                ReviewSession.user_id == current_user.id,
            )
            .with_for_update()
        )
        if session is None:
            mark_action_failed(db, action, "Review session not found")
            db.commit()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session not found")

        if session.status != "active":
            mark_action_ignored(
                db, action,
                response_payload={"progress": _progress_response(session).model_dump()},
                reason="session_not_active",
            )
            db.commit()
            return ReviewFeedbackResponse(
                done=False,
                next_item=None,
                summary=None,
                progress=_progress_response(session),
                status="ignored",
                ignored_reason="session_not_active",
            )

        # Step 4: Lock and validate session_item
        item = db.scalar(
            select(ReviewSessionItem)
            .where(
                ReviewSessionItem.id == payload.session_item_id,
                ReviewSessionItem.session_id == session.id,
            )
            .with_for_update()
        )
        if item is None:
            mark_action_failed(db, action, "Review session item not found")
            db.commit()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review session item not found")

        if item.status != "pending":
            mark_action_ignored(
                db, action,
                response_payload={"progress": _progress_response(session).model_dump()},
                reason="session_item_not_pending",
            )
            db.commit()
            return ReviewFeedbackResponse(
                done=False,
                next_item=None,
                summary=None,
                progress=_progress_response(session),
                status="ignored",
                ignored_reason="session_item_not_pending",
            )

        # Step 5: Validate card
        card = db.scalar(
            select(Card)
            .where(
                Card.id == payload.card_id,
                Card.user_id == current_user.id,
                Card.deleted_at.is_(None),
                Card.status == "active",
            )
            .with_for_update()
        )
        if card is None:
            mark_action_failed(db, action, "Card not found")
            db.commit()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Card not found")
        if item.card_id != card.id:
            mark_action_failed(db, action, "Session item does not match card")
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Session item does not match card",
            )

        # Step 6: Apply Phase 2 feedback rules
        transitions = apply_review_feedback_to_card(card, payload.result, now)

        # Step 7: Write review_log
        log = ReviewLog(
            user_id=current_user.id,
            card_id=card.id,
            session_id=session.id,
            session_item_id=item.id,
            session_type=session.session_type,
            result=payload.result,
            reviewed_at=payload.reviewed_at or now,
            card_state_before_review=transitions["review_state_before"],
            review_state_before=transitions["review_state_before"],
            review_state_after=transitions["review_state_after"],
            mastery_score_before=transitions["mastery_score_before"],
            mastery_score_after=transitions["mastery_score_after"],
            recovery_stage_before=transitions["recovery_stage_before"],
            recovery_stage_after=transitions["recovery_stage_after"],
            next_review_at_before=transitions["next_review_at_before"],
            next_review_at_after=transitions["next_review_at_after"],
        )
        db.add(log)

        # Step 8: Mark session_item reviewed
        item.status = "reviewed"
        item.result = payload.result
        item.reviewed_at = payload.reviewed_at or now
        item.final_result = payload.result
        item.first_result = item.first_result or payload.result

        # Step 9: Handle reappear items
        if should_append_reappear_item(payload.result, item.reappear_count):
            current_max_position = db.scalar(
                select(func.max(ReviewSessionItem.position)).where(
                    ReviewSessionItem.session_id == session.id
                )
            )
            current_max_position = int(current_max_position if current_max_position is not None else item.position)
            target_position = calculate_reappear_insert_position(item.position, current_max_position)
            _shift_items_for_insert(db, session.id, target_position, current_max_position)
            db.add(
                ReviewSessionItem(
                    session_id=session.id,
                    card_id=card.id,
                    position=target_position,
                    status="pending",
                    result=None,
                    reappear_count=item.reappear_count + 1,
                    is_repeat=True,
                    repeat_count=item.reappear_count + 1,
                    first_result=item.first_result or payload.result,
                )
            )

        # Step 10: Refresh session progress and find next item
        db.flush()
        _refresh_session_progress(db, session, now)
        next_item = _next_pending_item(db, session.id)
        summary = None if next_item is not None else _build_summary(db, session.id)
        progress = _progress_response(session)
        next_item_response = _item_response(next_item, now) if next_item is not None else None

        response = ReviewFeedbackResponse(
            done=next_item is None,
            next_item=next_item_response,
            summary=summary,
            progress=progress,
            status="success",
        )

        # Step 11: Mark succeeded
        mark_action_succeeded(db, action, response_payload=response.model_dump(mode="json"))
        db.commit()

        return response

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/sessions/{session_id}/summary", response_model=SessionSummaryResponse)
def get_session_summary(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SessionSummaryResponse:
    session = db.scalar(
        select(ReviewSession).where(
            ReviewSession.id == session_id,
            ReviewSession.user_id == current_user.id,
        )
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    progress = _progress_response(session)
    summary = _build_summary(db, session.id)

    return SessionSummaryResponse(
        session_id=session.id,
        status=session.status,
        progress=progress,
        summary=summary,
    )
