from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User, utc_now
from app.observability.operations import observed_operation
from app.schemas.card import (
    CardCreate,
    CardListResponse,
    CardResponse,
    CardStatsResponse,
    CardSyncRequest,
    CardSyncResponse,
    CardUpdate,
)
from app.services.auth_service import get_current_user
from app.services.card_service import (
    create_card,
    delete_card,
    get_card_or_404,
    get_cards_stats,
    list_cards,
    update_card,
)
from app.services.card_sync_service import sync_card_operation


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
    with observed_operation("database", "card_create"):
        return create_card(db, payload.model_copy(update={"user_id": current_user.id}))


@router.get("", response_model=CardListResponse)
def list_cards_endpoint(
    user_id: UUID | None = None,
    keyword: str | None = None,
    review_state: str | None = Query(default=None, pattern="^(new|strengthening|reviewing|mastered)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_deleted: bool = False,
    updated_since: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardListResponse:
    _ensure_payload_user_matches_current_user(current_user, user_id)
    with observed_operation("database", "card_list"):
        cards, total = list_cards(
            db,
            user_id=current_user.id,
            keyword=keyword,
            review_state=review_state,
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
            updated_since=updated_since,
        )

    sync_cursor = None
    server_time = utc_now().isoformat()
    if updated_since is not None:
        # Ordered by updated_at ASC, so the last item holds the newest change.
        sync_cursor = (
            cards[-1].updated_at.isoformat() if cards else updated_since.isoformat()
        )
    return CardListResponse(
        items=cards,
        total=total,
        limit=limit,
        offset=offset,
        sync_cursor=sync_cursor,
        server_time=server_time,
    )


@router.get("/stats", response_model=CardStatsResponse)
def get_card_stats_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardStatsResponse:
    with observed_operation("database", "card_stats"):
        return CardStatsResponse(**get_cards_stats(db, current_user.id))


@router.post("/sync", response_model=CardSyncResponse)
def sync_card_endpoint(
    payload: CardSyncRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardSyncResponse:
    with observed_operation("database", "card_sync"):
        return sync_card_operation(db, current_user.id, payload)


@router.get("/{card_id}", response_model=CardResponse)
def get_card_endpoint(
    card_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    with observed_operation("database", "card_get"):
        return get_card_or_404(db, card_id, current_user.id)


@router.patch("/{card_id}", response_model=CardResponse)
def update_card_endpoint(
    card_id: UUID,
    payload: CardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    with observed_operation("database", "card_update"):
        return update_card(db, card_id, current_user.id, payload)


@router.delete("/{card_id}", response_model=CardResponse)
def delete_card_endpoint(
    card_id: UUID,
    base_version: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardResponse:
    with observed_operation("database", "card_delete"):
        return delete_card(db, card_id, current_user.id, base_version)
