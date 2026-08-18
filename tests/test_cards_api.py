from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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

    def set_card_timestamp(self, card_id, updated_at, deleted=False):
        with TestingSessionLocal() as db:
            card = db.scalar(select(Card).where(Card.id == UUID(card_id)))
            card.updated_at = updated_at
            if deleted:
                card.deleted_at = updated_at
                card.status = "deleted"
            db.commit()

    def test_incremental_sync_returns_only_changed_cards_and_tombstone(self):
        t1 = datetime(2026, 8, 17, 10, 0, 0)
        t2 = datetime(2026, 8, 17, 10, 0, 10)
        t3 = datetime(2026, 8, 17, 10, 0, 20)

        a = self.create_card(content="alpha", local_temp_id="inc-alpha", legacy_cloud_id="inc-alpha-cloud")
        b = self.create_card(content="bravo", local_temp_id="inc-bravo", legacy_cloud_id="inc-bravo-cloud")
        self.set_card_timestamp(a["id"], t1)
        self.set_card_timestamp(b["id"], t2)

        # Full list (no updated_since) returns active cards only and no cursor.
        full = self.client.get("/api/cards", headers=self.auth_headers(), params={"user_id": self.user_id})
        self.assertEqual(200, full.status_code)
        self.assertEqual(2, full.json()["total"])
        self.assertIsNone(full.json()["sync_cursor"])

        # Soft-delete A after the last sync: updated_at bumps past the cursor.
        self.set_card_timestamp(a["id"], t3, deleted=True)

        # Incremental from the last cursor returns ONLY the changed (deleted) card.
        resp = self.client.get(
            "/api/cards",
            headers=self.auth_headers(),
            params={"user_id": self.user_id, "updated_since": t2.isoformat()},
        )
        self.assertEqual(200, resp.status_code)
        data = resp.json()
        ids = {item["id"] for item in data["items"]}
        self.assertEqual({a["id"]}, ids)
        self.assertIsNotNone(data["items"][0]["deleted_at"])
        self.assertEqual("deleted", data["items"][0]["status"])
        self.assertEqual(t3.isoformat(), data["sync_cursor"])

    def test_incremental_sync_empty_result_preserves_cursor(self):
        t1 = datetime(2001, 1, 1, 0, 0, 0)
        a = self.create_card(content="alpha", local_temp_id="inc-empty", legacy_cloud_id="inc-empty-cloud")
        self.set_card_timestamp(a["id"], t1)

        cursor = datetime(2001, 1, 1, 0, 1, 0).isoformat()
        resp = self.client.get(
            "/api/cards",
            headers=self.auth_headers(),
            params={"user_id": self.user_id, "updated_since": cursor},
        )
        self.assertEqual(200, resp.status_code)
        data = resp.json()
        self.assertEqual(0, data["total"])
        self.assertEqual([], data["items"])
        self.assertEqual(cursor, data["sync_cursor"])

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
        self.assertEqual(1, card["version"])

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
        self.assertEqual(created["version"] + 1, data["version"])

    def test_update_with_stale_base_version_returns_structured_conflict(self):
        created = self.create_card(local_temp_id="versioned-direct-card")
        first = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={"content": "first device edit", "base_version": created["version"]},
        )
        self.assertEqual(200, first.status_code, first.text)

        stale = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={"content": "stale device edit", "base_version": created["version"]},
        )
        self.assertEqual(409, stale.status_code, stale.text)
        detail = stale.json()["detail"]
        self.assertEqual("card_version_conflict", detail["code"])
        self.assertEqual("first device edit", detail["server_card"]["content"])
        self.assertEqual(first.json()["version"], detail["server_card"]["version"])

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

    def test_sync_create_is_idempotent_and_maps_local_id(self):
        request = {
            "client_action_id": "offline-create-action-1",
            "operation": "CREATE",
            "local_id": "offline-local-card-1",
            "payload": {
                "content": "created while offline",
                "card_type": "phrase",
                "translation": "离线创建",
            },
        }
        first = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=request)
        self.assertEqual(200, first.status_code, first.text)
        replay = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=request)
        self.assertEqual(200, replay.status_code, replay.text)

        self.assertEqual(first.json()["card"]["id"], replay.json()["card"]["id"])
        self.assertEqual("offline-local-card-1", first.json()["card"]["local_temp_id"])
        self.assertFalse(first.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
        listed = self.client.get("/api/cards", headers=self.auth_headers()).json()
        self.assertEqual(1, listed["total"])

    def test_sync_update_detects_two_device_version_conflict(self):
        created = self.create_card(local_temp_id="sync-version-card")
        first_request = {
            "client_action_id": "offline-update-action-1",
            "operation": "UPDATE",
            "local_id": "sync-version-card",
            "card_id": created["id"],
            "base_version": created["version"],
            "payload": {"content": "device one content"},
        }
        first = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=first_request)
        self.assertEqual(200, first.status_code, first.text)

        stale_request = {
            **first_request,
            "client_action_id": "offline-update-action-2",
            "payload": {"content": "device two stale content"},
        }
        stale = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=stale_request)
        self.assertEqual(409, stale.status_code, stale.text)
        detail = stale.json()["detail"]
        self.assertEqual("card_version_conflict", detail["code"])
        self.assertEqual("device one content", detail["server_card"]["content"])

        current = self.client.get(f"/api/cards/{created['id']}", headers=self.auth_headers())
        self.assertEqual("device one content", current.json()["content"])

    def test_sync_delete_persists_tombstone_and_is_idempotent(self):
        created = self.create_card(local_temp_id="sync-delete-card")
        request = {
            "client_action_id": "offline-delete-action-1",
            "operation": "DELETE",
            "local_id": "sync-delete-card",
            "card_id": created["id"],
            "base_version": created["version"],
        }
        first = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=request)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("deleted", first.json()["card"]["status"])
        self.assertIsNotNone(first.json()["card"]["deleted_at"])
        self.assertEqual(created["version"] + 1, first.json()["card"]["version"])

        replay = self.client.post("/api/cards/sync", headers=self.auth_headers(), json=request)
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertTrue(replay.json()["replayed"])

        active = self.client.get("/api/cards", headers=self.auth_headers()).json()
        self.assertEqual([], active["items"])
        snapshot = self.client.get(
            "/api/cards",
            headers=self.auth_headers(),
            params={"include_deleted": True},
        ).json()
        self.assertEqual(1, snapshot["total"])
        self.assertEqual("deleted", snapshot["items"][0]["status"])

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
            headers=self.auth_headers(),
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

    def test_context_fields_round_trip_through_card_crud(self):
        created = self.create_card(
            local_temp_id="local-context-1",
            legacy_cloud_id="cloud-context-1",
            source_context="The speaker urged us to make every opportunity count.",
            source_url="https://example.com/talk?t=90",
            example_sentence="Make your final semester count.",
            example_translation="让你的最后一个学期过得有价值。",
        )

        self.assertEqual(
            "The speaker urged us to make every opportunity count.",
            created["source_context"],
        )
        self.assertEqual("https://example.com/talk?t=90", created["source_url"])
        self.assertEqual("Make your final semester count.", created["example_sentence"])
        self.assertEqual("让你的最后一个学期过得有价值。", created["example_translation"])

        response = self.client.patch(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
            json={
                "source_context": "This expression appeared in the closing paragraph.",
                "source_url": "https://example.com/article#closing",
                "example_sentence": "Small choices can make the day count.",
                "example_translation": "微小的选择也能让这一天更有意义。",
            },
        )

        self.assertEqual(200, response.status_code, response.text)
        updated = response.json()
        self.assertEqual(
            "This expression appeared in the closing paragraph.",
            updated["source_context"],
        )
        self.assertEqual("https://example.com/article#closing", updated["source_url"])
        self.assertEqual("Small choices can make the day count.", updated["example_sentence"])
        self.assertEqual("微小的选择也能让这一天更有意义。", updated["example_translation"])

        detail = self.client.get(
            f"/api/cards/{created['id']}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        self.assertEqual(updated["source_context"], detail.json()["source_context"])

    def test_source_url_rejects_values_longer_than_database_column(self):
        response = self.client.post(
            "/api/cards",
            headers=self.auth_headers(),
            json={
                "content": "keep someone in the loop",
                "card_type": "phrase",
                "source_url": "x" * 1001,
            },
        )

        self.assertEqual(422, response.status_code, response.text)

    def test_context_fields_round_trip_through_card_sync(self):
        create_request = {
            "client_action_id": "context-sync-create-1",
            "operation": "CREATE",
            "local_id": "context-sync-local-1",
            "payload": {
                "content": "in the loop",
                "card_type": "phrase",
                "source_context": "Please keep everyone in the loop as the plan changes.",
                "source_url": "https://example.com/email/42",
                "example_sentence": "Keep me in the loop about the interview.",
                "example_translation": "请及时告诉我面试的进展。",
            },
        }
        created_response = self.client.post(
            "/api/cards/sync",
            headers=self.auth_headers(),
            json=create_request,
        )
        self.assertEqual(200, created_response.status_code, created_response.text)
        created = created_response.json()["card"]
        self.assertEqual(
            "Please keep everyone in the loop as the plan changes.",
            created["source_context"],
        )
        self.assertEqual("https://example.com/email/42", created["source_url"])

        update_response = self.client.post(
            "/api/cards/sync",
            headers=self.auth_headers(),
            json={
                "client_action_id": "context-sync-update-1",
                "operation": "UPDATE",
                "local_id": "context-sync-local-1",
                "card_id": created["id"],
                "base_version": created["version"],
                "payload": {
                    "source_context": "The manager kept us in the loop throughout the delay.",
                    "example_sentence": "A short update will keep the team in the loop.",
                    "example_translation": "简短的更新能让团队及时了解情况。",
                },
            },
        )
        self.assertEqual(200, update_response.status_code, update_response.text)
        updated = update_response.json()["card"]
        self.assertEqual(
            "The manager kept us in the loop throughout the delay.",
            updated["source_context"],
        )
        self.assertEqual(
            "A short update will keep the team in the loop.",
            updated["example_sentence"],
        )
        self.assertEqual("简短的更新能让团队及时了解情况。", updated["example_translation"])

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

if __name__ == "__main__":
    unittest.main()
