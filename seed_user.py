from datetime import datetime
from uuid import UUID

from app.database import SessionLocal
from app.models.user import User

TEST_USER_ID = UUID("11111111-1111-1111-1111-111111111111")

db = SessionLocal()

try:
    user = db.get(User, TEST_USER_ID)

    if user is None:
        user = User(
            id=TEST_USER_ID,
            wx_openid="test-openid-orm-001",
            wx_unionid=None,
            nickname="test-user",
            avatar_url=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            last_login_at=datetime.now(),
        )
        db.add(user)
        db.commit()
        print("测试用户已创建:", TEST_USER_ID)
    else:
        print("测试用户已存在:", TEST_USER_ID)

finally:
    db.close()