from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


CardType = Literal["word", "phrase", "sentence"]
AnalysisStatus = Literal["pending", "done", "failed"]
AnalysisLevel = Literal["pass", "warning", "error"]
CardStatus = Literal["active", "archived", "deleted"]
ReviewState = Literal["new", "strengthening", "reviewing", "mastered"]
UnderstandingSource = Literal["local", "machine", "ai", "user"]


class CardCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    user_id: UUID | None = None
    content: str = Field(validation_alias=AliasChoices("content", "english_text"))
    card_type: CardType = "word"
    legacy_cloud_id: str | None = None
    local_temp_id: str | None = None
    exam_scene: str | None = None
    exam_module: str | None = None
    understanding: str | None = Field(
        default=None,
        validation_alias=AliasChoices("understanding", "user_understanding", "ai_understanding"),
    )
    note: str | None = Field(default=None, validation_alias=AliasChoices("note", "notes"))
    where_encountered: str | None = None
    translation: str | None = Field(
        default=None,
        validation_alias=AliasChoices("translation", "meaning_cn", "context_translation"),
    )
    analysis_status: AnalysisStatus = "pending"
    analysis_level: AnalysisLevel = "pass"
    analysis_messages: list[str] = Field(default_factory=list)
    understanding_source: UnderstandingSource = "user"
    next_review_at: datetime | None = None


class CardUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    content: str | None = Field(default=None, validation_alias=AliasChoices("content", "english_text"))
    understanding: str | None = Field(
        default=None,
        validation_alias=AliasChoices("understanding", "user_understanding", "ai_understanding"),
    )
    note: str | None = Field(default=None, validation_alias=AliasChoices("note", "notes"))
    where_encountered: str | None = None
    translation: str | None = Field(
        default=None,
        validation_alias=AliasChoices("translation", "meaning_cn", "context_translation"),
    )
    analysis_status: AnalysisStatus | None = None
    card_type: CardType | None = None
    exam_scene: str | None = None
    exam_module: str | None = None


class CardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    legacy_cloud_id: str | None = None
    local_temp_id: str | None = None
    content: str
    content_normalized: str
    card_type: CardType
    exam_scene: str | None = None
    exam_module: str | None = None
    understanding: str | None = None
    note: str | None = None
    where_encountered: str | None = None
    translation: str | None = None
    analysis_status: AnalysisStatus
    is_review_ready: bool
    needs_manual_fix: bool
    analysis_level: AnalysisLevel
    analysis_messages: list[str] = Field(default_factory=list)
    understanding_source: UnderstandingSource
    review_state: ReviewState
    mastery_score: int
    recovery_stage: int
    review_count: int
    first_reviewed_at: datetime | None = None
    again_count: int
    hard_count: int
    good_count: int
    easy_count: int
    forgot_count: int
    shaky_count: int
    got_it_count: int
    fluent_count: int
    last_review_result: str | None = None
    last_reviewed_at: datetime | None = None
    next_review_at: datetime | None = None
    status: CardStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    @computed_field
    @property
    def english_text(self) -> str:
        return self.content

    @computed_field
    @property
    def normalized_text(self) -> str:
        return self.content_normalized


class CardListResponse(BaseModel):
    items: list[CardResponse]
    total: int
    limit: int
    offset: int


class CardStatsResponse(BaseModel):
    total: int
    new: int
    reviewing: int
    strengthening: int
    mastered: int
    needs_manual_fix: int
    pending: int
