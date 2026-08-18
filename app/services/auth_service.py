from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.user import User, utc_now
from app.schemas.auth import AuthUserResponse, DEFAULT_TIMEZONE, WechatLoginResponse


WECHAT_CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
TOKEN_SUBJECT_KEY = "sub"
bearer_scheme = HTTPBearer(auto_error=False)


def _require_wechat_config() -> None:
    if not settings.wechat_appid or not settings.wechat_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Wechat login is not configured",
        )


def _require_jwt_config() -> None:
    if not settings.jwt_secret_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret is not configured",
        )


def request_wechat_code2session(code: str) -> dict[str, Any]:
    _require_wechat_config()
    response = httpx.get(
        WECHAT_CODE2SESSION_URL,
        params={
            "appid": settings.wechat_appid,
            "secret": settings.wechat_secret,
            "js_code": code,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def create_access_token(user_id: UUID) -> str:
    _require_jwt_config()
    now = datetime.now(timezone.utc)
    payload = {
        TOKEN_SUBJECT_KEY: str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> UUID:
    _require_jwt_config()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        subject = payload.get(TOKEN_SUBJECT_KEY)
        if not subject:
            raise ValueError("Missing token subject")
        return UUID(str(subject))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def login_with_wechat_code(db: Session, code: str, timezone_name: str = DEFAULT_TIMEZONE) -> WechatLoginResponse:
    try:
        session_data = request_wechat_code2session(code)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Wechat login request failed",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Wechat login service is unavailable",
        ) from exc

    errcode = session_data.get("errcode")
    if errcode:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Wechat login failed: {session_data.get('errmsg', 'unknown error')}",
        )

    openid = str(session_data.get("openid") or "").strip()
    if not openid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wechat login failed: openid missing",
        )

    unionid = str(session_data.get("unionid") or "").strip() or None
    user = db.scalar(select(User).where(User.wx_openid == openid))
    is_new_user = user is None
    now = utc_now()
    normalized_timezone = (timezone_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE

    if user is None:
        user = User(
            wx_openid=openid,
            wx_unionid=unionid,
            timezone=normalized_timezone,
            last_login_at=now,
        )
        db.add(user)
    else:
        user.last_login_at = now
        user.timezone = normalized_timezone
        if unionid:
            user.wx_unionid = unionid

    db.commit()
    db.refresh(user)

    return WechatLoginResponse(
        user_id=user.id,
        access_token=create_access_token(user.id),
        is_new_user=is_new_user,
        user=AuthUserResponse(id=user.id, timezone=user.timezone),
    )


def _ensure_user_can_authenticate(user: User) -> User:
    if user.account_status != "active" or user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active",
        )
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported authorization scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_access_token(credentials.credentials)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.auth_scheme = "bearer"
    return _ensure_user_can_authenticate(user)
