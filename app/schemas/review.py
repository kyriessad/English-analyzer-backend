from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


ReviewResult = Literal["again", "hard", "good", "easy"]
CardBucket = Literal["all", "due", "weak", "mastered"]


class PreviewTodayRequest(BaseModel):
    review_date: date
    timezone: str
    cards: list[dict[str, Any]] = Field(default_factory=list)


class PreviewTodayItem(BaseModel):
    card: dict[str, Any]
    bucket: CardBucket


class PreviewTodayResponse(BaseModel):
    items: list[PreviewTodayItem]


class CalculateNextReviewRequest(BaseModel):
    card: dict[str, Any]
    result: ReviewResult
    reviewed_at: datetime
    timezone: str


class CalculateNextReviewResponse(BaseModel):
    next_review_at: datetime


class ClassifyCardRequest(BaseModel):
    card: dict[str, Any]
    today: date
    timezone: str


class ClassifyCardResponse(BaseModel):
    bucket: CardBucket


class ReviewSessionCardResponse(BaseModel):
    id: UUID
    content: str
    card_type: str
    review_count: int
    last_review_result: str | None = None
    next_review_at: datetime | None = None


class ReviewSessionItemResponse(BaseModel):
    id: UUID
    session_id: UUID
    card_id: UUID
    position: int
    status: Literal["pending", "done", "skipped"]
    is_repeat: bool
    repeat_count: int
    first_result: str | None = None
    final_result: str | None = None
    card: ReviewSessionCardResponse


class TodayReviewResponse(BaseModel):
    session_id: UUID
    review_date: date
    timezone: str
    status: Literal["active", "completed", "abandoned"]
    total_count: int
    completed_count: int
    current_index: int
    items: list[ReviewSessionItemResponse]


class ReviewFeedbackRequest(BaseModel):
    user_id: UUID | None = None
    session_id: UUID
    session_item_id: UUID
    card_id: UUID
    result: ReviewResult
    reviewed_at: datetime
    timezone: str
    client_record_id: str = Field(min_length=1)


class ReviewRecordResponse(BaseModel):
    id: UUID
    user_id: UUID
    card_id: UUID
    session_id: UUID | None = None
    session_item_id: UUID | None = None
    result: ReviewResult
    result_label: str
    reviewed_at: datetime
    review_date: date
    before_next_review_at: datetime | None = None
    after_next_review_at: datetime | None = None
    before_review_count: int
    after_review_count: int
    client_record_id: str
    source: str
    created_at: datetime


class CardReviewUpdateResponse(BaseModel):
    id: UUID
    review_count: int
    again_count: int
    hard_count: int
    good_count: int
    easy_count: int
    last_review_result: str | None = None
    last_reviewed_at: datetime | None = None
    next_review_at: datetime | None = None


class ReviewSessionUpdateResponse(BaseModel):
    session_id: UUID
    status: Literal["active", "completed", "abandoned"]
    total_count: int
    completed_count: int
    current_index: int
    completed_at: datetime | None = None


class ReviewFeedbackResponse(BaseModel):
    record: ReviewRecordResponse
    card_update: CardReviewUpdateResponse
    session_update: ReviewSessionUpdateResponse
    next_item: ReviewSessionItemResponse | None = None
    completed: bool
    idempotent: bool = False


class ReviewSummaryCardItem(BaseModel):
    card_id: UUID
    content: str
    final_result: str | None = None
    review_count: int


class ReviewSessionSummaryResponse(BaseModel):
    session_id: UUID
    review_date: date
    total_count: int
    completed_count: int
    again_count: int
    hard_count: int
    good_count: int
    easy_count: int
    items: list[ReviewSummaryCardItem]
