from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.auth import CurrentUserResponse, WechatLoginRequest, WechatLoginResponse
from app.services.auth_service import get_current_user, login_with_wechat_code, revoke_user_tokens


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/wechat-login", response_model=WechatLoginResponse)
def wechat_login(payload: WechatLoginRequest, db: Session = Depends(get_db)) -> WechatLoginResponse:
    return login_with_wechat_code(db, payload.code, payload.timezone)


@router.get("/me", response_model=CurrentUserResponse)
def get_me(current_user: User = Depends(get_current_user)) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=current_user.id,
        timezone=current_user.timezone,
        email=current_user.email,
        username=current_user.username,
        role=current_user.role,
        account_status=current_user.account_status,
        email_verified=current_user.email_verified_at is not None,
        daily_goal=current_user.daily_goal,
        pronunciation_voice=current_user.pronunciation_voice,
        created_at=current_user.created_at.isoformat(),
        last_login_at=current_user.last_login_at.isoformat() if current_user.last_login_at else None,
    )


@router.post("/logout")
def logout(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    revoke_user_tokens(db, current_user)
    return {"status": "logged_out"}
