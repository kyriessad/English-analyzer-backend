from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.review import (
    CalculateNextReviewRequest,
    CalculateNextReviewResponse,
    ClassifyCardRequest,
    ClassifyCardResponse,
    PreviewTodayItem,
    PreviewTodayRequest,
    PreviewTodayResponse,
    ReviewFeedbackRequest,
    ReviewFeedbackResponse,
    ReviewSessionSummaryResponse,
    TodayReviewResponse,
)
from app.services.auth_service import get_current_user_optional
from app.services.review_session_service import (
    get_or_create_today_session,
    get_review_session_summary,
    submit_review_feedback,
)
from app.services.review_scheduler import (
    calculate_next_review_at,
    classify_card_status,
    select_today_cards,
)


router = APIRouter(prefix="/api/review", tags=["review"])


def _resolve_user_id(current_user: User | None, fallback_user_id: UUID | None) -> UUID | None:
    return current_user.id if current_user is not None else fallback_user_id


@router.post("/preview-today", response_model=PreviewTodayResponse)
def preview_today(payload: PreviewTodayRequest) -> PreviewTodayResponse:
    selected_cards = select_today_cards(
        payload.cards,
        payload.review_date,
        payload.timezone,
    )
    items = [
        PreviewTodayItem(
            card=card,
            bucket=classify_card_status(card, payload.review_date, payload.timezone),
        )
        for card in selected_cards
    ]
    return PreviewTodayResponse(items=items)


@router.post("/calculate-next-review", response_model=CalculateNextReviewResponse)
def calculate_next_review(payload: CalculateNextReviewRequest) -> CalculateNextReviewResponse:
    next_review_at = calculate_next_review_at(
        payload.card,
        payload.result,
        payload.reviewed_at,
        payload.timezone,
    )
    return CalculateNextReviewResponse(next_review_at=next_review_at)


@router.post("/classify-card", response_model=ClassifyCardResponse)
def classify_card(payload: ClassifyCardRequest) -> ClassifyCardResponse:
    bucket = classify_card_status(payload.card, payload.today, payload.timezone)
    return ClassifyCardResponse(bucket=bucket)


@router.get("/today", response_model=TodayReviewResponse)
def get_today_review(
    review_date: date,
    timezone: str,
    user_id: UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
) -> TodayReviewResponse:
    resolved_user_id = _resolve_user_id(current_user, user_id)
    if resolved_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id is required when no bearer token is provided",
        )
    return get_or_create_today_session(
        db,
        user_id=resolved_user_id,
        review_date=review_date,
        timezone=timezone,
    )


@router.post("/feedback", response_model=ReviewFeedbackResponse)
def submit_feedback(
    payload: ReviewFeedbackRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
) -> ReviewFeedbackResponse:
    resolved_user_id = _resolve_user_id(current_user, payload.user_id)
    if resolved_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="user_id is required when no bearer token is provided",
        )
    return submit_review_feedback(db, payload.model_copy(update={"user_id": resolved_user_id}))


@router.get("/sessions/{session_id}/summary", response_model=ReviewSessionSummaryResponse)
def get_session_summary(
    session_id: UUID,
    db: Session = Depends(get_db),
) -> ReviewSessionSummaryResponse:
    return get_review_session_summary(db, session_id)
