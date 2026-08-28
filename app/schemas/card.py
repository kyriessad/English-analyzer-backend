from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field, model_validator


CardType = Literal["word", "phrase", "sentence"]
AnalysisStatus = Literal["pending", "done", "failed"]
AnalysisLevel = Literal["pass", "warning", "error"]
CardStatus = Literal["active", "archived", "deleted"]
ReviewState = Literal["new", "strengthening", "reviewing", "mastered"]
UnderstandingSource = Literal["local", "machine", "ai", "user"]


class CardCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    user_id: UUID | None = None
    content: str = Field(
        min_length=1,
        validation_alias=AliasChoices("content", "english_text"),
    )
    card_type: CardType = "word"
    legacy_cloud_id: str | None = None
    local_temp_id: str | None = None
    exam_scene: str | None = Field(default=None, max_length=100)
    exam_module: str | None = Field(default=None, max_length=100)
    understanding: str | None = Field(
        default=None,
        validation_alias=AliasChoices("understanding", "user_understanding", "ai_understanding"),
    )
    note: str | None = Field(
        default=None,
        max_length=4000,
        validation_alias=AliasChoices("note", "notes"),
    )
    where_encountered: str | None = Field(default=None, max_length=1000)
    source_context: str | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    example_sentence: str | None = None
    example_translation: str | None = None
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
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    base_version: int | None = Field(default=None, ge=1)

    content: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("content", "english_text"),
    )
    understanding: str | None = Field(
        default=None,
        validation_alias=AliasChoices("understanding", "user_understanding", "ai_understanding"),
    )
    note: str | None = Field(
        default=None,
        max_length=4000,
        validation_alias=AliasChoices("note", "notes"),
    )
    where_encountered: str | None = Field(default=None, max_length=1000)
    source_context: str | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    example_sentence: str | None = None
    example_translation: str | None = None
    translation: str | None = Field(
        default=None,
        validation_alias=AliasChoices("translation", "meaning_cn", "context_translation"),
    )
    analysis_status: AnalysisStatus | None = None
    analysis_level: AnalysisLevel | None = None
    analysis_messages: list[str] | None = None
    understanding_source: UnderstandingSource | None = None
    card_type: CardType | None = None
    exam_scene: str | None = Field(default=None, max_length=100)
    exam_module: str | None = Field(default=None, max_length=100)


class CardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version: int
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
    source_context: str | None = None
    source_url: str | None = None
    example_sentence: str | None = None
    example_translation: str | None = None
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
    sync_cursor: str | None = None
    server_time: str | None = None


CardSyncOperation = Literal["CREATE", "UPDATE", "DELETE"]


class CardSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_action_id: str = Field(min_length=8, max_length=128)
    operation: CardSyncOperation
    local_id: str = Field(min_length=1, max_length=128)
    card_id: UUID | None = None
    base_version: int | None = Field(default=None, ge=1)
    payload: dict | None = None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "CardSyncRequest":
        if self.operation == "CREATE":
            if self.payload is None:
                raise ValueError("CREATE requires payload")
        else:
            if self.card_id is None:
                raise ValueError(f"{self.operation} requires card_id")
            if self.base_version is None:
                raise ValueError(f"{self.operation} requires base_version")
        if self.operation == "UPDATE" and self.payload is None:
            raise ValueError("UPDATE requires payload")
        return self


class CardSyncResponse(BaseModel):
    operation: CardSyncOperation
    local_id: str
    card: CardResponse
    replayed: bool = False


class CardStatsResponse(BaseModel):
    total: int
    new: int
    reviewing: int
    strengthening: int
    mastered: int
    needs_manual_fix: int
    pending: int
