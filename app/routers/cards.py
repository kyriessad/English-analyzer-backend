from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.card import CardCreate, CardListResponse, CardResponse, CardUpdate
from app.services.auth_service import get_current_user
from app.services.card_service import create_card, delete_card, get_card_or_404, list_cards, update_card


router = APIRouter(prefix="/api/cards", tags=["cards"])


def _ensure_payload_user_matches_current_user(current_user: User, payload_user_id: UUID | None) -> None:
    if payload_user_id is not None and payload_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access cards for another user",
        )


@router.post("", response_model=CardResponse)
def create_card_endpoint(
    payload: CardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    _ensure_payload_user_matches_current_user(current_user, payload.user_id)
    return create_card(db, payload.model_copy(update={"user_id": current_user.id}))


@router.get("", response_model=CardListResponse)
def list_cards_endpoint(
    user_id: UUID | None = None,
    keyword: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardListResponse:
    _ensure_payload_user_matches_current_user(current_user, user_id)
    cards, total = list_cards(
        db,
        user_id=current_user.id,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return CardListResponse(items=cards, total=total, limit=limit, offset=offset)


@router.get("/{card_id}", response_model=CardResponse)
def get_card_endpoint(
    card_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    return get_card_or_404(db, card_id, current_user.id)


@router.patch("/{card_id}", response_model=CardResponse)
def update_card_endpoint(
    card_id: UUID,
    payload: CardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    return update_card(db, card_id, current_user.id, payload)


@router.delete("/{card_id}", response_model=CardResponse)
def delete_card_endpoint(
    card_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    return delete_card(db, card_id, current_user.id)
