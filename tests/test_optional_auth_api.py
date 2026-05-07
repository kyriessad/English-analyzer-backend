from dataclasses import replace
from uuid import uuid4
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import auth_service


TIMEZONE = "Asia/Shanghai"


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


class OptionalAuthApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="optional-auth-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.user_id = uuid4()
        with TestingSessionLocal() as db:
            db.add(User(id=self.user_id, wx_openid=f"openid-{self.user_id}"))
            db.commit()
        self.token = auth_service.create_access_token(self.user_id)

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_token_can_create_card_without_body_user_id(self):
        response = self.client.post(
            "/api/cards",
            headers=self.auth_headers(),
            json={
                "content": "token card",
                "card_type": "phrase",
                "local_temp_id": "token-local-card",
            },
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(str(self.user_id), data["user_id"])
        self.assertEqual("token card", data["content"])

    def test_body_user_id_must_match_token_user(self):
        response = self.client.post(
            "/api/cards",
            headers=self.auth_headers(),
            json={
                "user_id": str(uuid4()),
                "content": "wrong user",
                "card_type": "phrase",
                "local_temp_id": "wrong-user-card",
            },
        )

        self.assertEqual(403, response.status_code)

    def test_token_can_get_today_review_without_query_user_id(self):
        create_response = self.client.post(
            "/api/cards",
            headers=self.auth_headers(),
            json={
                "content": "token review card",
                "card_type": "phrase",
                "local_temp_id": "token-review-card",
            },
        )
        self.assertEqual(200, create_response.status_code, create_response.text)

        response = self.client.get(
            "/api/review/today",
            headers=self.auth_headers(),
            params={
                "review_date": "2026-05-06",
                "timezone": TIMEZONE,
            },
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total_count"])
        self.assertEqual("token review card", data["items"][0]["card"]["content"])

    def test_invalid_token_returns_401(self):
        response = self.client.get(
            "/api/cards",
            headers={"Authorization": "Bearer invalid-token"},
        )

        self.assertEqual(401, response.status_code)


if __name__ == "__main__":
    unittest.main()
