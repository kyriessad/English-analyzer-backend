from dataclasses import replace
from unittest.mock import patch
from uuid import UUID
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import auth_service


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class AuthApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            wechat_appid="test-appid",
            wechat_secret="test-secret",
            jwt_secret_key="test-jwt-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def test_new_user_login_creates_user_and_returns_token(self):
        with patch(
            "app.services.auth_service.request_wechat_code2session",
            return_value={"openid": "openid-new", "unionid": "union-new"},
        ):
            response = self.client.post("/api/auth/wechat-login", json={"code": "wx-code"})

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertTrue(data["is_new_user"])
        self.assertEqual("bearer", data["token_type"])
        self.assertTrue(data["access_token"])
        self.assertEqual(data["user_id"], data["user"]["id"])
        self.assertEqual("Asia/Shanghai", data["user"]["timezone"])

        user_id = UUID(data["user_id"])
        with TestingSessionLocal() as db:
            user = db.get(User, user_id)
            self.assertIsNotNone(user)
            self.assertEqual("openid-new", user.wx_openid)
            self.assertEqual("union-new", user.wx_unionid)
            self.assertEqual("Asia/Shanghai", user.timezone)

    def test_existing_user_login_does_not_create_duplicate_user(self):
        with TestingSessionLocal() as db:
            user = User(wx_openid="openid-existing")
            db.add(user)
            db.commit()
            db.refresh(user)
            existing_user_id = str(user.id)

        with patch(
            "app.services.auth_service.request_wechat_code2session",
            return_value={"openid": "openid-existing"},
        ):
            response = self.client.post("/api/auth/wechat-login", json={"code": "wx-code"})

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertFalse(data["is_new_user"])
        self.assertEqual(existing_user_id, data["user_id"])

    def test_me_returns_current_user(self):
        with patch(
            "app.services.auth_service.request_wechat_code2session",
            return_value={"openid": "openid-me"},
        ):
            login_response = self.client.post(
                "/api/auth/wechat-login",
                json={"code": "wx-code", "timezone": "Asia/Shanghai"},
            )

        self.assertEqual(200, login_response.status_code, login_response.text)
        token = login_response.json()["access_token"]

        response = self.client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(login_response.json()["user_id"], data["id"])
        self.assertEqual("Asia/Shanghai", data["timezone"])
        self.assertTrue(data["created_at"])

        with TestingSessionLocal() as db:
            user_count = db.scalar(select(func.count()).select_from(User))
            self.assertEqual(1, user_count)

    def test_wechat_errcode_returns_error(self):
        with patch(
            "app.services.auth_service.request_wechat_code2session",
            return_value={"errcode": 40029, "errmsg": "invalid code"},
        ):
            response = self.client.post("/api/auth/wechat-login", json={"code": "bad-code"})

        self.assertEqual(401, response.status_code)
        self.assertIn("Wechat login failed", response.json()["detail"])

    def test_blank_code_returns_validation_error(self):
        response = self.client.post("/api/auth/wechat-login", json={"code": "   "})

        self.assertEqual(422, response.status_code)

    def test_access_token_can_be_decoded_to_user_id(self):
        with patch(
            "app.services.auth_service.request_wechat_code2session",
            return_value={"openid": "openid-token"},
        ):
            response = self.client.post("/api/auth/wechat-login", json={"code": "wx-code"})

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        decoded_user_id = auth_service.decode_access_token(data["access_token"])

        self.assertEqual(UUID(data["user_id"]), decoded_user_id)


if __name__ == "__main__":
    unittest.main()
