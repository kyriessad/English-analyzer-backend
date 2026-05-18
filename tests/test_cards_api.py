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
from app.models.card import Card
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


class CardsApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="cards-api-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.user_uuid = uuid4()
        self.user_id = str(self.user_uuid)
        self.other_user_uuid = uuid4()

        with TestingSessionLocal() as db:
            db.add(User(id=self.user_uuid, wx_openid=f"openid-{self.user_id}"))
            db.add(User(id=self.other_user_uuid, wx_openid=f"openid-{self.other_user_uuid}"))
            db.commit()
        self.token = auth_service.create_access_token(self.user_uuid)
        self.other_token = auth_service.create_access_token(self.other_user_uuid)

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def auth_headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def create_card(self, **overrides):
        payload = {
            "user_id": self.user_id,
            "content": "look forward to",
            "card_type": "phrase",
            "local_temp_id": "local-card-1",
            "legacy_cloud_id": "cloud-card-1",
            "understanding": "expect something",
            "note": "to is a preposition",
        }
        payload.update(overrides)
        response = self.client.post("/api/cards", headers=self.auth_headers(), json=payload)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_create_card_successfully(self):
        card = self.create_card()

        self.assertEqual(self.user_id, card["user_id"])
        self.assertEqual("look forward to", card["content"])
        self.assertEqual("active", card["status"])
        self.assertEqual("pending", card["analysis_status"])
        self.assertEqual(0, card["review_count"])
        self.assertEqual(0, card["again_count"])
        self.assertEqual(0, card["hard_count"])
        self.assertEqual(0, card["good_count"])
        self.assertEqual(0, card["easy_count"])
        self.assertEqual("local-card-1", card["local_temp_id"])
        self.assertEqual("cloud-card-1", card["legacy_cloud_id"])
        self.assertTrue(card["is_review_ready"])
        self.assertFalse(card["needs_manual_fix"])
        self.assertIsNotNone(card["next_review_at"])

    def test_create_card_with_same_local_temp_id_returns_existing_card(self):
        created = self.create_card()

        duplicate = self.create_card(
            content="changed content",
            legacy_cloud_id="cloud-card-duplicate",
        )

        self.assertEqual(created["id"], duplicate["id"])
        self.assertEqual("look forward to", duplicate["content"])

        response = self.client.get("/api/cards", headers=self.auth_headers(), params={"user_id": self.user_id})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["total"])

    def test_list_cards_returns_created_card(self):
        created = self.create_card()

        response = self.client.get("/api/cards", headers=self.auth_headers(), params={"user_id": self.user_id})

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual(created["id"], data["items"][0]["id"])

    def test_get_card_detail_successfully(self):
        created = self.create_card()

        response = self.client.get(f"/api/cards/{created['id']}", headers=self.auth_headers())

        self.assertEqual(200, response.status_code)
        self.assertEqual(created["id"], response.json()["id"])

    def test_update_card_successfully_and_resets_analysis_status_when_content_changes(self):
        created = self.create_card()

        response = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={
                "content": "pay attention to",
                "understanding": "focus on something",
                "note": "updated note",
                "card_type": "phrase",
                "exam_scene": "考研",
                "exam_module": "阅读",
            },
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("pay attention to", data["content"])
        self.assertEqual("pay attention to", data["content_normalized"])
        self.assertEqual("focus on something", data["understanding"])
        self.assertEqual("updated note", data["note"])
        self.assertEqual("pending", data["analysis_status"])
        self.assertEqual("考研", data["exam_scene"])
        self.assertEqual("阅读", data["exam_module"])

    def test_patch_failed_not_ready_card_recomputes_readiness(self):
        # Phase 6G: content alone → is_review_ready=True; needs_manual_fix always False
        created = self.create_card(
            local_temp_id="local-needs-fix",
            legacy_cloud_id="cloud-needs-fix",
            understanding=None,
            translation=None,
            analysis_status="failed",
        )
        self.assertTrue(created["is_review_ready"])
        self.assertFalse(created["needs_manual_fix"])

        response = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={"user_understanding": "need to fix this meaning"},
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertTrue(data["is_review_ready"])
        self.assertFalse(data["needs_manual_fix"])
        self.assertEqual("failed", data["analysis_status"])

    def test_cards_stats_counts_main_states_and_readiness_flags(self):
        with TestingSessionLocal() as db:
            for review_state in ("new", "reviewing", "strengthening", "mastered"):
                db.add(
                    Card(
                        user_id=self.user_uuid,
                        content=f"{review_state} card",
                        content_normalized=f"{review_state} card",
                        card_type="word",
                        understanding="meaning",
                        analysis_status="done",
                        is_review_ready=True,
                        needs_manual_fix=False,
                        analysis_level="pass",
                        analysis_messages=[],
                        understanding_source="user",
                        review_state=review_state,
                        status="active",
                    )
                )
            db.add(
                Card(
                    user_id=self.user_uuid,
                    content="pending card",
                    content_normalized="pending card",
                    card_type="word",
                    understanding="meaning",
                    analysis_status="pending",
                    is_review_ready=True,
                    needs_manual_fix=False,
                    analysis_level="pass",
                    analysis_messages=[],
                    understanding_source="user",
                    review_state="new",
                    status="active",
                )
            )
            db.add(
                Card(
                    user_id=self.user_uuid,
                    content="manual fix card",
                    content_normalized="manual fix card",
                    card_type="word",
                    analysis_status="failed",
                    is_review_ready=False,
                    needs_manual_fix=True,
                    analysis_level="error",
                    analysis_messages=[],
                    understanding_source="user",
                    review_state="new",
                    status="active",
                )
            )
            db.commit()

        response = self.client.get("/api/cards/stats", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(6, data["total"])
        self.assertEqual(3, data["new"])
        self.assertEqual(1, data["reviewing"])
        self.assertEqual(1, data["strengthening"])
        self.assertEqual(1, data["mastered"])
        self.assertEqual(1, data["needs_manual_fix"])
        self.assertEqual(1, data["pending"])

    def test_delete_card_soft_deletes(self):
        created = self.create_card()

        response = self.client.delete(f"/api/cards/{created['id']}", headers=self.auth_headers())

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual("deleted", data["status"])
        self.assertIsNotNone(data["deleted_at"])

    def test_default_list_does_not_return_deleted_cards(self):
        created = self.create_card()
        self.client.delete(f"/api/cards/{created['id']}", headers=self.auth_headers())

        response = self.client.get("/api/cards", headers=self.auth_headers(), params={"user_id": self.user_id})

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(0, data["total"])
        self.assertEqual([], data["items"])

    def test_keyword_searches_content(self):
        created = self.create_card(content="make a difference", local_temp_id="local-keyword")
        self.create_card(content="look up", local_temp_id="local-other", legacy_cloud_id="cloud-other")

        response = self.client.get(
            "/api/cards",
            headers=self.auth_headers(),
            params={"user_id": self.user_id, "keyword": "difference"},
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual(created["id"], data["items"][0]["id"])

    def test_cards_require_bearer_token(self):
        response = self.client.get("/api/cards")

        self.assertEqual(401, response.status_code)

    def test_user_cannot_access_another_users_card(self):
        created = self.create_card()

        response = self.client.get(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(self.other_token),
        )

        self.assertEqual(404, response.status_code)

    def test_analyze_english_still_works(self):
        response = self.client.post(
            "/api/analyze-english",
            json={"text": "你好", "cardType": "auto", "targetLang": "zh"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("error", response.json()["level"])

    def test_create_card_with_where_encountered(self):
        card = self.create_card(
            local_temp_id="local-where-1",
            legacy_cloud_id="cloud-where-1",
            where_encountered="NBA 解说",
        )

        self.assertEqual("NBA 解说", card["where_encountered"])

    def test_update_card_where_encountered(self):
        created = self.create_card(
            local_temp_id="local-where-2",
            legacy_cloud_id="cloud-where-2",
            where_encountered="工作邮件",
        )
        self.assertEqual("工作邮件", created["where_encountered"])

        response = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={"where_encountered": "美剧 Friends"},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("美剧 Friends", response.json()["where_encountered"])

    def test_create_card_without_where_encountered_is_null(self):
        card = self.create_card(
            local_temp_id="local-where-3",
            legacy_cloud_id="cloud-where-3",
        )

        self.assertIsNone(card["where_encountered"])
        self.assertEqual("look forward to", card["content"])
        self.assertIn("exam_scene", card)  # exam_scene field still present and unaffected

    def test_review_preview_api_still_works(self):
        response = self.client.post(
            "/api/review/preview-today",
            json={
                "review_date": "2026-05-06",
                "timezone": "Asia/Shanghai",
                "cards": [{"id": "new", "status": "active", "review_count": 0}],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(["new"], [item["card"]["id"] for item in response.json()["items"]])


if __name__ == "__main__":
    unittest.main()
