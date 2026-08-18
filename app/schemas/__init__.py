"""
schemas 是“合同”，规定前后端交流的数据格式。
"""
from typing import Literal

from pydantic import BaseModel, Field


Level = Literal["pass", "warning", "error", "failed"]
Category = Literal["word", "phrase", "sentence", "paragraph", "unknown"]


# 前端/云函数要传什么字段
class AnalyzeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    cardType: str = "auto"
    targetLang: str = "zh"
    forceRefresh: bool = False


# 后端会返回什么字段
class AnalyzeResponse(BaseModel):
    ok: bool
    level: Level
    category: Category
    normalizedText: str
    translation: str | None = None
    understanding: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    provider: str | None = None
    cacheHit: bool = False
    exampleSentence: str | None = None
    exampleTranslation: str | None = None
    exampleSource: str | None = None
    exampleError: str | None = None
    synonyms: list[dict] = Field(default_factory=list)
    similarPhrases: list[dict] = Field(default_factory=list)
    expressionType: str | None = None
    alternativeMeanings: list[dict] = Field(default_factory=list)
    usageScenario: str | None = None
    dialogue: dict | None = None
    analysisSource: str | None = None
    analysisModel: str | None = None
