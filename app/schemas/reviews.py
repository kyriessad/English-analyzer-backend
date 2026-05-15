from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


ReviewResult = Literal["forgot", "shaky", "got_it", "fluent"]
ReviewState = Literal["new", "strengthening", "reviewing", "mastered"]
ReviewSessionType = Literal["daily_suggested", "new_only", "free_review"]
ReviewSessionStatus = Literal["active", "completed", "abandoned", "expired"]


class ReviewSuggestedOverview(BaseModel):
    review_count: int
    new_count: int
    strengthening_count: int
    due_count: int
    total_count: int


class ReviewCompletedSuggestedOverview(BaseModel):
    review_count: int
    new_count: int
    total_count: int


class ReviewExtraTodayOverview(BaseModel):
    new_only_count: int
    free_review_count: int
    total_count: int


class ActiveReviewSessionResponse(BaseModel):
    id: UUID
    session_type: ReviewSessionType
    remaining_count: int
    total_count: int
    reviewed_count: int
    status: ReviewSessionStatus


class ReviewOverviewResponse(BaseModel):
    suggested: ReviewSuggestedOverview
    completed_suggested: ReviewCompletedSuggestedOverview
    extra_today: ReviewExtraTodayOverview
    is_all_done: bool
    active_session: ActiveReviewSessionResponse | None = None


class ReviewProgressResponse(BaseModel):
    reviewed: int
    total: int


class ReviewItemResponse(BaseModel):
    session_item_id: UUID
    card_id: UUID
    content: str
    understanding: str | None = ""
    note: str | None = ""
    card_type: str
    review_state: ReviewState
    mastery_score: int
    recovery_stage: int
    due_reason: str


class TodayReviewsResponse(BaseModel):
    session_id: UUID | None = None
    limit: int
    progress: ReviewProgressResponse
    items: list[ReviewItemResponse]


class ReviewSessionCreateRequest(BaseModel):
    session_type: ReviewSessionType = "daily_suggested"
    limit: int = 5
    restart: bool = False


class ReviewSessionCreateResponse(BaseModel):
    session_id: UUID | None = None
    session_type: ReviewSessionType
    status: ReviewSessionStatus | None = None
    limit: int
    planned_new_count: int = 0
    planned_review_count: int = 0
    progress: ReviewProgressResponse
    items: list[ReviewItemResponse]


class ReviewFeedbackRequest(BaseModel):
    client_action_id: str
    session_id: UUID
    session_item_id: UUID
    card_id: UUID
    result: ReviewResult
    reviewed_at: datetime | None = None


class ReviewSummaryResponse(BaseModel):
    unique_card_count: int
    total_review_count: int
    forgot: int
    shaky: int
    got_it: int
    fluent: int
    strengthening_count: int
    mastered_count: int


class ReviewFeedbackResponse(BaseModel):
    done: bool
    next_item: ReviewItemResponse | None = None
    summary: ReviewSummaryResponse | None = None
    progress: ReviewProgressResponse
    status: str = "success"
    ignored_reason: str | None = None


class SessionSummaryResponse(BaseModel):
    session_id: UUID
    status: str
    progress: ReviewProgressResponse
    summary: ReviewSummaryResponse


class ReviewHistoryItemResponse(BaseModel):
    review_log_id: UUID
    card_id: UUID
    content: str = ""
    understanding: str | None = None
    note: str | None = None
    card_type: str | None = None
    exam_scene: str | None = None
    exam_module: str | None = None
    review_count_in_range: int
    last_result: ReviewResult
    last_result_label: str
    last_reviewed_at: datetime
    card_source: str = "current_card"


class ReviewHistoryResponse(BaseModel):
    items: list[ReviewHistoryItemResponse]
    total: int
    limit: int
    offset: int


class ReviewHistoryResultCounts(BaseModel):
    forgot: int = 0
    shaky: int = 0
    got_it: int = 0
    fluent: int = 0


class ReviewHistorySummaryResponse(BaseModel):
    total_reviews: int
    unique_cards: int
    latest_result_card_counts: ReviewHistoryResultCounts
    date_from: date | None = None
    date_to: date | None = None


class ReviewLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    card_id: UUID
    session_id: UUID
    session_item_id: UUID
    session_type: ReviewSessionType
    result: ReviewResult
    reviewed_at: datetime
    card_state_before_review: ReviewState
    review_state_before: str
    review_state_after: str
    mastery_score_before: int
    mastery_score_after: int
    recovery_stage_before: int
    recovery_stage_after: int
    next_review_at_before: datetime | None = None
    next_review_at_after: datetime | None = None


class ReviewHistoryDetailCardResponse(BaseModel):
    id: UUID
    card_id: UUID
    content: str
    understanding: str | None = None
    note: str | None = None
    card_type: str | None = None
    exam_scene: str | None = None
    exam_module: str | None = None
    review_state: str | None = None
    next_review_at: datetime | None = None
    card_source: str = "current_card"


class ReviewHistoryDetailResponse(BaseModel):
    id: UUID
    review_log_id: UUID
    reviewed_at: datetime
    result: str
    result_label: str
    session_type: str
    session_type_label: str
    card: ReviewHistoryDetailCardResponse | None = None


class TodayReviewedItem(BaseModel):
    card_id: UUID
    content: str
    understanding: str | None = ""
    note: str | None = ""
    card_type: str
    exam_scene: str | None = None
    exam_module: str | None = None
    today_review_count: int
    last_result: ReviewResult
    last_result_label: str
    last_reviewed_at: datetime


class TodayReviewedResponse(BaseModel):
    items: list[TodayReviewedItem]
    total: int
