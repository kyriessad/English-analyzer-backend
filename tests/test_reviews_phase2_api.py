from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.core.config import settings as app_settings
from app.database import Base, get_db
from app.main import app
from app.models.card import Card
from app.models.review import ClientAction, ReviewLog, ReviewSession, ReviewSessionItem
from app.models.user import User
from app.services import auth_service


NOW = datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc)

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


class ReviewsPhase2ApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="reviews-phase2-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.user_uuid = uuid4()
        self.other_user_uuid = uuid4()
        with TestingSessionLocal() as db:
            db.add(User(id=self.user_uuid, wx_openid=f"openid-{self.user_uuid}"))
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
        values = {
            "user_id": self.user_uuid,
            "content": f"card {uuid4()}",
            "content_normalized": f"card {uuid4()}",
            "card_type": "word",
            "understanding": "meaning",
            "analysis_status": "done",
            "is_review_ready": True,
            "needs_manual_fix": False,
            "analysis_level": "pass",
            "analysis_messages": [],
            "understanding_source": "user",
            "review_state": "new",
            "mastery_score": 0,
            "recovery_stage": 0,
            "review_count": 0,
            "forgot_count": 0,
            "shaky_count": 0,
            "got_it_count": 0,
            "fluent_count": 0,
            "again_count": 0,
            "hard_count": 0,
            "good_count": 0,
            "easy_count": 0,
            "next_review_at": None,
            "status": "active",
        }
        values.update(overrides)
        with TestingSessionLocal() as db:
            card = Card(**values)
            db.add(card)
            db.commit()
            db.refresh(card)
            return card.id

    def test_overview_requires_token_and_does_not_count_all_new_cards_as_required(self):
        for index in range(20):
            self.create_card(content=f"new {index}", content_normalized=f"new {index}")
        self.create_card(review_state="strengthening")
        self.create_card(review_state="reviewing", next_review_at=NOW - timedelta(days=1))
        self.create_card(user_id=self.other_user_uuid, review_state="strengthening")

        unauthorized = self.client.get("/api/reviews/overview")
        self.assertEqual(401, unauthorized.status_code)

        response = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(2, data["suggested"]["review_count"])
        self.assertEqual(1, data["suggested"]["new_count"])
        self.assertEqual(1, data["suggested"]["strengthening_count"])
        self.assertEqual(1, data["suggested"]["due_count"])
        self.assertEqual(3, data["suggested"]["total_count"])
        self.assertEqual(0, data["completed_suggested"]["total_count"])
        self.assertEqual(0, data["extra_today"]["total_count"])
        self.assertFalse(data["is_all_done"])
        self.assertIsNone(data["active_session"])

    def test_today_creates_reuses_and_restarts_session(self):
        self.create_card(content="older new", content_normalized="older new")
        self.create_card(content="newer new", content_normalized="newer new")

        first = self.client.get("/api/reviews/today?limit=5", headers=self.auth_headers())
        self.assertEqual(200, first.status_code, first.text)
        first_data = first.json()
        self.assertIsNotNone(first_data["session_id"])
        self.assertEqual(2, first_data["progress"]["total"])
        self.assertEqual(2, len(first_data["items"]))

        second = self.client.get("/api/reviews/today?limit=5", headers=self.auth_headers())
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(first_data["session_id"], second.json()["session_id"])

        restarted = self.client.get("/api/reviews/today?limit=5&restart=true", headers=self.auth_headers())
        self.assertEqual(200, restarted.status_code, restarted.text)
        self.assertNotEqual(first_data["session_id"], restarted.json()["session_id"])

        with TestingSessionLocal() as db:
            old_session = db.get(ReviewSession, UUID(first_data["session_id"]))
            self.assertEqual("abandoned", old_session.status)

    def test_post_review_sessions_switch_requires_restart_and_abandons_old_active(self):
        self.create_card(content="daily card", content_normalized="daily card")
        self.create_card(content="new only card", content_normalized="new only card")

        first = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5},
        )
        self.assertEqual(200, first.status_code, first.text)
        first_data = first.json()
        self.assertEqual("daily_suggested", first_data["session_type"])

        conflict = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5},
        )
        self.assertEqual(409, conflict.status_code, conflict.text)

        restarted = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )
        self.assertEqual(200, restarted.status_code, restarted.text)
        restarted_data = restarted.json()
        self.assertEqual("new_only", restarted_data["session_type"])
        self.assertNotEqual(first_data["session_id"], restarted_data["session_id"])

        with TestingSessionLocal() as db:
            old_session = db.get(ReviewSession, UUID(first_data["session_id"]))
            self.assertEqual("abandoned", old_session.status)

    def test_review_session_selection_filters_pending_and_manual_fix_cards(self):
        self.create_card(content="ready done", content_normalized="ready done")
        self.create_card(
            content="pending card",
            content_normalized="pending card",
            analysis_status="pending",
            is_review_ready=True,
            needs_manual_fix=False,
        )
        self.create_card(
            content="failed not ready",
            content_normalized="failed not ready",
            understanding=None,
            analysis_status="failed",
            is_review_ready=False,
            needs_manual_fix=True,
        )
        self.create_card(
            content="failed ready",
            content_normalized="failed ready",
            analysis_status="failed",
            is_review_ready=True,
            needs_manual_fix=False,
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        contents = {item["content"] for item in response.json()["items"]}
        self.assertEqual({"ready done", "failed ready"}, contents)

    def test_overview_separates_daily_suggested_from_extra_new_only(self):
        daily_card_id = self.create_card(content="daily new", content_normalized="daily new")
        daily = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "restart": True},
        ).json()

        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": daily["session_id"],
                "session_item_id": daily["items"][0]["session_item_id"],
                "card_id": str(daily_card_id),
                "result": "got_it",
            },
        )

        extra_card_id = self.create_card(content="extra new", content_normalized="extra new")
        extra = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        ).json()
        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": extra["session_id"],
                "session_item_id": extra["items"][0]["session_item_id"],
                "card_id": str(extra_card_id),
                "result": "got_it",
            },
        )

        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())

        self.assertEqual(200, overview.status_code, overview.text)
        data = overview.json()
        self.assertEqual(1, data["completed_suggested"]["new_count"])
        self.assertEqual(1, data["completed_suggested"]["total_count"])
        self.assertEqual(1, data["extra_today"]["new_only_count"])
        self.assertEqual(1, data["extra_today"]["total_count"])

    def test_overview_hides_active_session_with_zero_total_count(self):
        with TestingSessionLocal() as db:
            session = ReviewSession(
                user_id=self.user_uuid,
                review_date=NOW.date(),
                timezone="Asia/Shanghai",
                session_type="daily_suggested",
                status="active",
                batch_size=5,
                total_count=0,
                reviewed_count=0,
                completed_count=0,
                current_index=0,
            )
            db.add(session)
            db.commit()
            session_id = session.id

        response = self.client.get("/api/reviews/overview", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(response.json()["active_session"])
        with TestingSessionLocal() as db:
            session = db.get(ReviewSession, session_id)
            self.assertEqual("abandoned", session.status)

    def test_overview_hides_active_session_with_zero_remaining_count(self):
        card_id = self.create_card(content="already reviewed", content_normalized="already reviewed")
        with TestingSessionLocal() as db:
            session = ReviewSession(
                user_id=self.user_uuid,
                review_date=NOW.date(),
                timezone="Asia/Shanghai",
                session_type="daily_suggested",
                status="active",
                batch_size=5,
                total_count=1,
                reviewed_count=1,
                completed_count=1,
                current_index=1,
            )
            db.add(session)
            db.flush()
            db.add(
                ReviewSessionItem(
                    session_id=session.id,
                    card_id=card_id,
                    position=0,
                    status="reviewed",
                    result="got_it",
                    reappear_count=0,
                    is_repeat=False,
                    repeat_count=0,
                    first_result="got_it",
                    final_result="got_it",
                )
            )
            db.commit()
            session_id = session.id

        response = self.client.get("/api/reviews/overview", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(response.json()["active_session"])
        with TestingSessionLocal() as db:
            session = db.get(ReviewSession, session_id)
            self.assertEqual("completed", session.status)

    def _start_session(self):
        """Helper: start a review session and return session data + first item."""
        self.create_card(content="card a", content_normalized="card a")
        today = self.client.get("/api/reviews/today?limit=5", headers=self.auth_headers()).json()
        return today, today["items"][0]

    def test_feedback_forgot_updates_card_logs_and_reappear(self):
        """Phase 3: feedback with client_action_id succeeds."""
        card_id = self.create_card(content="forgot card", content_normalized="forgot card")
        today = self.client.get("/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()).json()
        self.assertEqual(1, len(today["items"]))
        item = today["items"][0]

        client_action_id = str(uuid4())
        response = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "forgot",
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertFalse(data["done"])
        self.assertEqual(1, data["progress"]["reviewed"])
        self.assertEqual("success", data["status"])

        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            self.assertEqual("strengthening", card.review_state)
            self.assertEqual(1, card.forgot_count)
            logs = list(db.scalars(select(ReviewLog).where(ReviewLog.card_id == card_id)))
            self.assertEqual(1, len(logs))
            self.assertEqual("daily_suggested", logs[0].session_type)
            self.assertEqual("new", logs[0].card_state_before_review)
            # Verify client_action was recorded
            ca = db.scalar(select(ClientAction).where(ClientAction.client_action_id == client_action_id))
            self.assertIsNotNone(ca)
            self.assertEqual("succeeded", ca.status)

    def test_same_client_action_id_returns_cached_response(self):
        """Phase 3: same client_action_id submitted twice — only processed once."""
        today, item = self._start_session()
        client_action_id = str(uuid4())

        payload = {
            "client_action_id": client_action_id,
            "session_id": today["session_id"],
            "session_item_id": item["session_item_id"],
            "card_id": item["card_id"],
            "result": "forgot",
        }

        first = self.client.post("/api/reviews/feedback", headers=self.auth_headers(), json=payload)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("success", first.json()["status"])

        second = self.client.post("/api/reviews/feedback", headers=self.auth_headers(), json=payload)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual("success", second.json()["status"])

        # Verify only one review_log was created
        with TestingSessionLocal() as db:
            logs = list(db.scalars(select(ReviewLog).where(ReviewLog.card_id == UUID(item["card_id"]))))
            self.assertEqual(1, len(logs))
            actions = list(db.scalars(select(ClientAction).where(ClientAction.client_action_id == client_action_id)))
            self.assertEqual(1, len(actions))

    def test_different_client_action_id_same_session_item_returns_ignored(self):
        """Phase 3: same session_item with different client_action_id → 200 ignored."""
        today, item = self._start_session()

        first_id = str(uuid4())
        first = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": first_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": "forgot",
            },
        )
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("success", first.json()["status"])

        second_id = str(uuid4())
        second = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": second_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": "got_it",
            },
        )
        self.assertEqual(200, second.status_code, second.text)
        data = second.json()
        self.assertEqual("ignored", data["status"])
        self.assertEqual("session_item_not_pending", data["ignored_reason"])

        # Verify only one review_log exists
        with TestingSessionLocal() as db:
            logs = list(db.scalars(select(ReviewLog).where(ReviewLog.card_id == UUID(item["card_id"]))))
            self.assertEqual(1, len(logs))

    def test_completed_session_old_action_returns_ignored(self):
        """Phase 3: action for completed session → 200 ignored."""
        self.create_card(content="card x", content_normalized="card x")
        today, item = self._start_session()

        # Complete the session
        ca_id = str(uuid4())
        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": ca_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": "fluent",
            },
        )

        # Try submitting another action against the now-completed session
        stale_ca_id = str(uuid4())
        resp = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": stale_ca_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": "got_it",
            },
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual("ignored", data["status"])

    def test_summary_endpoint_returns_session_summary(self):
        """Phase 3: GET /sessions/{session_id}/summary returns correct data."""
        self.create_card(content="summary card", content_normalized="summary card")
        today, item = self._start_session()

        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": "fluent",
            },
        )

        summary_resp = self.client.get(
            f"/api/reviews/sessions/{today['session_id']}/summary",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, summary_resp.status_code, summary_resp.text)
        data = summary_resp.json()
        self.assertEqual(today["session_id"], data["session_id"])
        self.assertIn(data["status"], ("active", "completed"))
        self.assertIn("progress", data)
        self.assertIn("summary", data)
        self.assertGreaterEqual(data["progress"]["total"], 1)

    def test_old_result_is_rejected_by_new_feedback_api(self):
        card_id = self.create_card()
        today = self.client.get("/api/reviews/today?limit=5", headers=self.auth_headers()).json()
        item = today["items"][0]

        response = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "again",
            },
        )

        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
