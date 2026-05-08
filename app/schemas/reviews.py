from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


ReviewResult = Literal["forgot", "shaky", "got_it", "fluent"]
ReviewState = Literal["new", "strengthening", "reviewing", "mastered"]


class ReviewOverviewResponse(BaseModel):
    today_required_count: int
    suggested_batch_size: int
    strengthening_count: int
    due_count: int
    new_available_count: int


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


class ReviewLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    card_id: UUID
    session_id: UUID
    session_item_id: UUID
    result: ReviewResult
    reviewed_at: datetime
    review_state_before: str
    review_state_after: str
    mastery_score_before: int
    mastery_score_after: int
    recovery_stage_before: int
    recovery_stage_after: int
    next_review_at_before: datetime | None = None
    next_review_at_after: datetime | None = None
