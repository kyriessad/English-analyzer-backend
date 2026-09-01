from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.discovery import MaterialItemListResponse, MaterialPackListResponse, MaterialStateRequest, MaterialStateResponse, TodayQuoteResponse
from app.services.auth_service import get_current_user
from app.services.discovery_service import DISCOVERY_TIMEZONE, get_today_quote, list_material_items, list_material_packs, set_material_known


router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/packs", response_model=MaterialPackListResponse)
def get_material_packs(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> MaterialPackListResponse:
    return MaterialPackListResponse(items=list_material_packs(db, current_user))


@router.get("/items", response_model=MaterialItemListResponse)
def get_material_items(
    pack: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    include_known: bool = False,
    q: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MaterialItemListResponse:
    items, total = list_material_items(
        db, current_user, pack_code=pack, limit=limit, offset=offset,
        include_known=include_known, query=q,
    )
    return MaterialItemListResponse(items=items, total=total, limit=limit, offset=offset)


@router.put("/items/{item_id}/state", response_model=MaterialStateResponse)
def update_material_state(
    item_id: UUID,
    payload: MaterialStateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MaterialStateResponse:
    return MaterialStateResponse(item_id=item_id, known=set_material_known(db, current_user, item_id, payload.known))


@router.get("/today-quote", response_model=TodayQuoteResponse)
def today_quote(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> TodayQuoteResponse:
    display_date, item = get_today_quote(db, current_user)
    return TodayQuoteResponse(display_date=display_date, timezone=DISCOVERY_TIMEZONE, item=item)
