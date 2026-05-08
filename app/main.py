"""
后端有哪些接口，main.py 是“门口”，外部请求先到这里。
"""
from fastapi import FastAPI

from app.routers.auth import router as auth_router
from app.routers.cards import router as cards_router
from app.routers.review import router as review_router
from app.routers.reviews import review_sessions_router, router as reviews_router
from app.schemas import AnalyzeRequest, AnalyzeResponse
from app.services.analyzer import analyze_text


app = FastAPI(
    title="English Analyzer Backend",
    version="0.1.0",
)
app.include_router(auth_router)
app.include_router(cards_router)
app.include_router(review_router)
app.include_router(reviews_router)
app.include_router(review_sessions_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/analyze-english", response_model=AnalyzeResponse)
def analyze_english(payload: AnalyzeRequest) -> AnalyzeResponse:
    result = analyze_text(
        text=payload.text,
        card_type=payload.cardType,
        target_lang=payload.targetLang,
    )
    return AnalyzeResponse(**result)
