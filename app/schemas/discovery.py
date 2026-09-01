from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class MaterialPackResponse(BaseModel):
    id: UUID
    code: str
    title: str
    description: str
    kind: Literal["word_book", "expression"]
    item_count: int
    remaining_count: int


class MaterialPackListResponse(BaseModel):
    items: list[MaterialPackResponse]


class MaterialItemResponse(BaseModel):
    id: UUID
    pack_id: UUID
    pack_code: str
    pack_title: str
    content: str
    chinese: str
    card_type: Literal["word", "phrase", "sentence"]
    source_label: str
    known: bool = False
    in_library: bool = False


class MaterialItemListResponse(BaseModel):
    items: list[MaterialItemResponse]
    total: int
    limit: int
    offset: int


class MaterialStateRequest(BaseModel):
    known: bool


class MaterialStateResponse(BaseModel):
    item_id: UUID
    known: bool


class TodayQuoteResponse(BaseModel):
    display_date: date
    timezone: str
    item: MaterialItemResponse
