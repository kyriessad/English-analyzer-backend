from uuid import UUID

from pydantic import BaseModel, Field, field_validator

DEFAULT_TIMEZONE = "Asia/Shanghai"


class WechatLoginRequest(BaseModel):
    code: str = Field(min_length=1)
    timezone: str = DEFAULT_TIMEZONE
    device_id: str | None = None

    @field_validator("code")
    @classmethod
    def code_must_not_be_blank(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("code is required")
        return code

    @field_validator("timezone")
    @classmethod
    def timezone_must_not_be_blank(cls, value: str) -> str:
        timezone = value.strip() if value else DEFAULT_TIMEZONE
        return timezone or DEFAULT_TIMEZONE


class AuthUserResponse(BaseModel):
    id: UUID
    timezone: str = DEFAULT_TIMEZONE


class WechatLoginResponse(BaseModel):
    user_id: UUID
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool
    user: AuthUserResponse


class CurrentUserResponse(AuthUserResponse):
    email: str | None = None
    username: str | None = None
    role: str = "user"
    account_status: str = "active"
    email_verified: bool = False
    daily_goal: int = 5
    pronunciation_voice: str = "male"
    created_at: str
    last_login_at: str | None = None
