from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models.card import Card
from app.models.review import ReviewLog, ReviewSession, ReviewSessionItem
from app.models.user import User, utc_now
from app.schemas.reviews import (
    ReviewFeedbackRequest,
    ReviewFeedbackResponse,
    ReviewHistoryDetailCardResponse,
    ReviewHistoryDetailResponse,
    ReviewHistoryItemResponse,
    ReviewHistoryResponse,
    ReviewHistoryResultCounts,
    ReviewHistorySummaryResponse,
    ReviewItemResponse,
    ReviewOverviewResponse,
    ReviewProgressResponse,
    ReviewResult,
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
    is_zombie_processing,
    mark_action_failed,
    mark_action_ignored,
    mark_action_succeeded,
    reset_zombie_processing,
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
REVIEW_RESULT_LABELS = {
    "forgot": "想不起来",
    "shaky": "不太稳",
    "got_it": "基本掌握",
    "fluent": "很熟了",
}

SESSION_TYPE_LABELS = {
    "daily_suggested": "系统今日推荐",
    "new_only": "主动新学",
    "free_review": "自由复习",
}


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
        # Phase 6G: content-based readiness — cards with non-empty content are always ready
        func.length(func.trim(Card.content)) > 0,
    ]


def _local_review_date(now: datetime, timezone_name: str) -> date:
    try:
        return now.astimezone(ZoneInfo(timezone_name or "UTC")).date()
    except Exception:
        return now.date()


def _history_date_bounds(
    date_from: date | None,
    date_to: date | None,
    timezone_name: str,
) -> tuple[datetime | None, datetime | None]:
    try:
        user_timezone = ZoneInfo(timezone_name or "UTC")
    except Exception:
        user_timezone = timezone.utc

    start_at = None
    end_before = None
    if date_from is not None:
        start_at = datetime.combine(date_from, time.min, tzinfo=user_timezone).astimezone(timezone.utc)
    if date_to is not None:
        end_before = (
            datetime.combine(date_to, time.min, tzinfo=user_timezone) + timedelta(days=1)
        ).astimezone(timezone.utc)

    return start_at, end_before


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


def _build_card_snapshot(card: Card) -> dict:
    """Capture stable card content at review time, before feedback mutates Card state."""
    return {
        "card_id": str(card.id),
        "content": card.content,
        "understanding": card.understanding,
        "note": card.note,
        "card_type": card.card_type,
        "exam_scene": card.exam_scene,
        "exam_module": card.exam_module,
        "analysis_status": card.analysis_status,
        "analysis_level": card.analysis_level,
    }


def _review_history_item_response(row) -> ReviewHistoryItemResponse:
    snapshot = getattr(row, "card_snapshot", None) or {}
    card_source = "snapshot" if snapshot else ("current_card" if row.content else "missing")

    content = snapshot.get("content") or row.content or ""
    understanding = snapshot.get("understanding") or row.understanding
    note = snapshot.get("note") or row.note
    card_type = snapshot.get("card_type") or row.card_type
    exam_scene = snapshot.get("exam_scene") or row.exam_scene
    exam_module = snapshot.get("exam_module") or row.exam_module

    return ReviewHistoryItemResponse(
        review_log_id=row.review_log_id,
        card_id=row.card_id,
        content=content,
        understanding=understanding,
        note=note,
        card_type=card_type,
        exam_scene=exam_scene,
        exam_module=exam_module,
        review_count_in_range=row.review_count_in_range,
        last_result=row.last_result,
        last_result_label=REVIEW_RESULT_LABELS[row.last_result],
        last_reviewed_at=row.last_reviewed_at,
        card_source=card_source,
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
    """Phase 6L-hotfix: free_review is the last-resort fallback.

    Select ALL eligible cards (any review_state), not only strengthening + due.
    This ensures the degradation chain daily_suggested -> new_only -> free_review
    always finds cards when the user's card library has any reviewable content.
    """
    return list(
        db.scalars(
            select(Card)
            .where(*_review_ready_card_filters(user_id))
            .order_by(Card.created_at, Card.id)
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
    suggested_new_count = new_available_count
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
    if daily_session is not None:
        _refresh_session_progress(db, daily_session, now)

    # Phase 6L-hotfix-4: 首页"今日任务" = 当前有效且属今日任务集合的唯一卡片数。
    # 若有今日 daily_suggested session（active 或 completed），以 session items 中
    # 仍存活的卡片数为准（自动过滤已删除卡）；否则以当前 review-ready 卡片数为准。
    # 上限 normalize_review_limit(5)，与 select_review_cards 一致。
    if daily_session is not None and daily_session.status in ("active", "completed"):
        suggested_total_count = min(
            db.scalar(
                select(func.count(func.distinct(ReviewSessionItem.card_id)))
                .select_from(ReviewSessionItem)
                .join(Card, Card.id == ReviewSessionItem.card_id)
                .where(
                    ReviewSessionItem.session_id == daily_session.id,
                    *_active_card_filters(current_user.id),
                )
            ) or 0,
            normalize_review_limit(5),
        )
    else:
        suggested_total_count = min(suggested_new_count + suggested_review_count, normalize_review_limit(5))

    # 今日已完成 = 今天已获得至少一次反馈的现存唯一卡片数。
    # JOIN Card 过滤已删除/非活跃卡片，避免孤儿 ReviewLog 污染统计。
    # JOIN ReviewSession 限定今日 daily_suggested session。
    # 按 card_id 去重，不计 reappear 重复反馈次数。
    completed_new_count = db.scalar(
        select(func.count(func.distinct(ReviewLog.card_id)))
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .join(Card, Card.id == ReviewLog.card_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "daily_suggested",
            ReviewSession.review_date == today,
            ReviewLog.card_state_before_review == "new",
            *_active_card_filters(current_user.id),
        )
    ) or 0
    completed_review_count = db.scalar(
        select(func.count(func.distinct(ReviewLog.card_id)))
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .join(Card, Card.id == ReviewLog.card_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "daily_suggested",
            ReviewSession.review_date == today,
            ReviewLog.card_state_before_review != "new",
            *_active_card_filters(current_user.id),
        )
    ) or 0

    completed_unique = db.scalar(
        select(func.count(func.distinct(ReviewLog.card_id)))
        .select_from(ReviewLog)
        .join(ReviewSession, ReviewSession.id == ReviewLog.session_id)
        .join(Card, Card.id == ReviewLog.card_id)
        .where(
            ReviewLog.user_id == current_user.id,
            ReviewLog.session_type == "daily_suggested",
            ReviewSession.review_date == today,
            *_active_card_filters(current_user.id),
        )
    ) or 0
    free_review_available_count = db.scalar(
        select(func.count())
        .select_from(Card)
        .where(
            *base_filters,
            Card.review_state != "new",
            or_(
                Card.review_state == "strengthening",
                Card.next_review_at <= now,
            ),
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

    completed_suggested_total = completed_unique
    # is_all_done 仅在以下情况为 true：
    # 1. 根本没有待处理卡片 (suggested_total_count == 0)
    # 2. 无活跃 session 且所有待处理卡片均已完成
    # 不再因为 daily_session.status == "completed" 自动标为全完成，
    # 避免新增卡后首页仍显示"今日已完成"。
    is_all_done = (
        suggested_total_count == 0
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
            "new_only_count": new_available_count,
            "free_review_count": free_review_available_count,
            "total_count": new_available_count + free_review_available_count,
        },
        is_all_done=is_all_done,
        active_session=active_session_response,
    )


@router.get("/history", response_model=ReviewHistoryResponse)
def get_review_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    date_from: date | None = None,
    date_to: date | None = None,
    result: list[ReviewResult] | None = Query(default=None),
    search: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewHistoryResponse:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be earlier than or equal to date_to",
        )

    start_at, end_before = _history_date_bounds(date_from, date_to, current_user.timezone)
    log_filters = [ReviewLog.user_id == current_user.id]
    if start_at is not None:
        log_filters.append(ReviewLog.reviewed_at >= start_at)
    if end_before is not None:
        log_filters.append(ReviewLog.reviewed_at < end_before)

    filtered_logs = (
        select(
            ReviewLog.id.label("review_log_id"),
            ReviewLog.card_id.label("card_id"),
            ReviewLog.result.label("result"),
            ReviewLog.reviewed_at.label("reviewed_at"),
            ReviewLog.card_snapshot.label("card_snapshot"),
            func.count(ReviewLog.id).over(partition_by=ReviewLog.card_id).label("review_count_in_range"),
            func.row_number()
            .over(
                partition_by=ReviewLog.card_id,
                order_by=(ReviewLog.reviewed_at.desc(), ReviewLog.id.desc()),
            )
            .label("row_number"),
        )
        .where(*log_filters)
        .cte("filtered_review_logs")
    )
    latest_logs = (
        select(
            filtered_logs.c.review_log_id,
            filtered_logs.c.card_id,
            filtered_logs.c.result.label("last_result"),
            filtered_logs.c.reviewed_at.label("last_reviewed_at"),
            filtered_logs.c.review_count_in_range,
            filtered_logs.c.card_snapshot,
        )
        .where(filtered_logs.c.row_number == 1)
        .cte("latest_review_logs")
    )

    card_join = latest_logs.outerjoin(
        Card,
        and_(
            Card.id == latest_logs.c.card_id,
            Card.user_id == current_user.id,
        ),
    )
    history_filters = [
        or_(
            Card.id.is_(None),
            and_(
                Card.deleted_at.is_(None),
                Card.status == "active",
            ),
        )
    ]
    if result:
        history_filters.append(latest_logs.c.last_result.in_(result))

    normalized_search = search.strip() if search else None
    if normalized_search:
        pattern = f"%{normalized_search}%"
        history_filters.append(
            or_(
                Card.content.ilike(pattern),
                Card.understanding.ilike(pattern),
                Card.note.ilike(pattern),
                Card.exam_scene.ilike(pattern),
                Card.exam_module.ilike(pattern),
            )
        )

    history_query = (
        select(
            latest_logs.c.review_log_id,
            latest_logs.c.card_id,
            Card.content,
            Card.understanding,
            Card.note,
            Card.card_type,
            Card.exam_scene,
            Card.exam_module,
            latest_logs.c.review_count_in_range,
            latest_logs.c.last_result,
            latest_logs.c.last_reviewed_at,
            latest_logs.c.card_snapshot,
        )
        .select_from(card_join)
        .where(*history_filters)
    )

    total = db.scalar(select(func.count()).select_from(history_query.subquery())) or 0
    rows = db.execute(
        history_query
        .order_by(latest_logs.c.last_reviewed_at.desc(), latest_logs.c.card_id)
        .offset(offset)
        .limit(limit)
    ).all()

    return ReviewHistoryResponse(
        items=[_review_history_item_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/history/summary", response_model=ReviewHistorySummaryResponse)
def get_review_history_summary(
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewHistorySummaryResponse:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be earlier than or equal to date_to",
        )

    start_at, end_before = _history_date_bounds(date_from, date_to, current_user.timezone)
    log_filters = [ReviewLog.user_id == current_user.id]
    if start_at is not None:
        log_filters.append(ReviewLog.reviewed_at >= start_at)
    if end_before is not None:
        log_filters.append(ReviewLog.reviewed_at < end_before)

    normalized_search = search.strip() if search else None
    if normalized_search:
        pattern = f"%{normalized_search}%"
        matching_card_ids = select(Card.id).where(
            Card.user_id == current_user.id,
            or_(
                Card.content.ilike(pattern),
                Card.understanding.ilike(pattern),
                Card.note.ilike(pattern),
                Card.exam_scene.ilike(pattern),
                Card.exam_module.ilike(pattern),
            )
        )
        log_filters.append(ReviewLog.card_id.in_(matching_card_ids))

    total_reviews = db.scalar(
        select(func.count()).select_from(ReviewLog).where(*log_filters)
    ) or 0
    unique_cards = db.scalar(
        select(func.count(func.distinct(ReviewLog.card_id))).select_from(ReviewLog).where(*log_filters)
    ) or 0

    filtered_logs = (
        select(
            ReviewLog.card_id.label("card_id"),
            ReviewLog.result.label("result"),
            func.row_number()
            .over(
                partition_by=ReviewLog.card_id,
                order_by=(ReviewLog.reviewed_at.desc(), ReviewLog.id.desc()),
            )
            .label("row_number"),
        )
        .where(*log_filters)
        .cte("filtered_summary_review_logs")
    )
    latest_logs = (
        select(
            filtered_logs.c.card_id,
            filtered_logs.c.result,
        )
        .where(filtered_logs.c.row_number == 1)
        .cte("latest_summary_review_logs")
    )

    counts = {"forgot": 0, "shaky": 0, "got_it": 0, "fluent": 0}
    count_rows = db.execute(
        select(latest_logs.c.result, func.count())
        .select_from(latest_logs)
        .group_by(latest_logs.c.result)
    ).all()
    for result_value, count in count_rows:
        if result_value in counts:
            counts[result_value] = count

    return ReviewHistorySummaryResponse(
        total_reviews=total_reviews,
        unique_cards=unique_cards,
        latest_result_card_counts=ReviewHistoryResultCounts(**counts),
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/history/{log_id}", response_model=ReviewHistoryDetailResponse)
def get_review_history_detail(
    log_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewHistoryDetailResponse:
    log = db.scalar(
        select(ReviewLog).where(
            ReviewLog.id == log_id,
            ReviewLog.user_id == current_user.id,
        )
    )

    if log is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review log not found",
        )

    card_response = None
    card_source = "missing"

    if log.card_snapshot:
        s = log.card_snapshot
        card_response = ReviewHistoryDetailCardResponse(
            id=log.card_id,
            card_id=log.card_id,
            content=s.get("content", ""),
            understanding=s.get("understanding"),
            note=s.get("note"),
            card_type=s.get("card_type"),
            exam_scene=s.get("exam_scene"),
            exam_module=s.get("exam_module"),
            review_state=None,
            next_review_at=None,
            card_source="snapshot",
        )
        card_source = "snapshot"
    else:
        card_obj = db.scalar(
            select(Card).where(
                Card.id == log.card_id,
                Card.user_id == current_user.id,
                Card.deleted_at.is_(None),
                Card.status == "active",
            )
        )

        if card_obj is not None:
            card_response = ReviewHistoryDetailCardResponse(
                id=card_obj.id,
                card_id=card_obj.id,
                content=card_obj.content,
                understanding=card_obj.understanding,
                note=card_obj.note,
                card_type=card_obj.card_type,
                exam_scene=card_obj.exam_scene,
                exam_module=card_obj.exam_module,
                review_state=card_obj.review_state,
                next_review_at=card_obj.next_review_at,
                card_source="current_card",
            )
            card_source = "current_card"

    session_type_label = SESSION_TYPE_LABELS.get(log.session_type, "其他来源")

    return ReviewHistoryDetailResponse(
        id=log.id,
        review_log_id=log.id,
        reviewed_at=log.reviewed_at,
        result=log.result,
        result_label=REVIEW_RESULT_LABELS.get(log.result, log.result),
        session_type=log.session_type,
        session_type_label=session_type_label,
        card=card_response,
    )


@router.get("/today", response_model=TodayReviewsResponse)
def get_today_reviews(
    limit: int = Query(default=5),
    restart: bool = Query(default=False),
    session_type: str = Query(default="daily_suggested"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TodayReviewsResponse:
    normalized_limit = normalize_review_limit(limit)
    now = utc_now()

    session, pending_items = _create_or_return_session(
        db,
        user=current_user,
        session_type=session_type,
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
    action = None
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
            if is_zombie_processing(existing_action):
                # Zombie processing: re-arm and take over
                action = reset_zombie_processing(
                    db,
                    existing_action,
                    new_request_payload=payload.model_dump(mode="json"),
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Action is being processed, please retry later.",
                )

    # Step 2: Create processing record if needed (skip for zombie reuse)
    try:
        if action is None:
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

        # Step 6: Capture card snapshot BEFORE feedback mutates Card state
        card_snapshot = _build_card_snapshot(card)

        # Step 7: Apply Phase 2 feedback rules
        transitions = apply_review_feedback_to_card(card, payload.result, now)

        # Step 8: Write review_log
        log = ReviewLog(
            user_id=current_user.id,
            card_id=card.id,
            session_id=session.id,
            session_item_id=item.id,
            session_type=session.session_type,
            result=payload.result,
            reviewed_at=payload.reviewed_at or now,
            card_snapshot=card_snapshot,
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

        # Step 9: Mark session_item reviewed
        item.status = "reviewed"
        item.result = payload.result
        item.reviewed_at = payload.reviewed_at or now
        item.final_result = payload.result
        item.first_result = item.first_result or payload.result

        # Step 10: Handle reappear items
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

        # Step 11: Refresh session progress and find next item
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

        # Step 12: Mark succeeded
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
