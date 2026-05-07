from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


CardType = Literal["word", "phrase", "sentence"]
AnalysisStatus = Literal["pending", "done", "failed"]
AnalysisLevel = Literal["pass", "warning", "error"]
CardStatus = Literal["active", "archived", "deleted"]
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
    understanding: str | None = None
    note: str | None = None
    translation: str | None = None
    analysis_status: AnalysisStatus = "pending"
    analysis_level: AnalysisLevel = "pass"
    analysis_messages: list[str] = Field(default_factory=list)
    understanding_source: UnderstandingSource = "user"
    next_review_at: datetime | None = None


class CardUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    content: str | None = Field(default=None, validation_alias=AliasChoices("content", "english_text"))
    understanding: str | None = None
    note: str | None = None
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
    translation: str | None = None
    analysis_status: AnalysisStatus
    analysis_level: AnalysisLevel
    analysis_messages: list[str] = Field(default_factory=list)
    understanding_source: UnderstandingSource
    review_count: int
    again_count: int
    hard_count: int
    good_count: int
    easy_count: int
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
