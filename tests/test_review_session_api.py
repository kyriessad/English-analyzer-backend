from datetime import datetime
from dataclasses import replace
from uuid import UUID, uuid4
import unittest
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models
from app.database import Base, get_db
from app.main import app
from app.models.card import Card
from app.models.review import ReviewRecord, ReviewSession, ReviewSessionItem
from app.models.user import User
from app.services import auth_service


TIMEZONE = "Asia/Shanghai"
REVIEW_DATE = "2026-05-06"
REVIEWED_AT = "2026-05-06T20:30:00+08:00"


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


class ReviewSessionApiTest(unittest.TestCase):
    def setUp(self):
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="review-session-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.reset_database()

    def reset_database(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.user_uuid = uuid4()
        self.user_id = str(self.user_uuid)

        with TestingSessionLocal() as db:
            db.add(User(id=self.user_uuid, wx_openid=f"openid-{self.user_id}"))
            db.commit()
        self.token = auth_service.create_access_token(self.user_uuid)

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def create_card_via_api(self, **overrides):
        payload = {
            "user_id": self.user_id,
            "content": "look forward to",
            "card_type": "phrase",
            "local_temp_id": f"local-{uuid4()}",
        }
        payload.update(overrides)
        response = self.client.post(
            "/api/cards",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def create_mastered_future_card(self, content="already mastered"):
        with TestingSessionLocal() as db:
            card = Card(
                user_id=self.user_uuid,
                content=content,
                content_normalized=content,
                card_type="phrase",
                analysis_status="done",
                analysis_level="pass",
                analysis_messages=[],
                understanding_source="user",
                review_count=3,
                again_count=0,
                hard_count=0,
                good_count=2,
                easy_count=1,
                last_review_result="easy",
                next_review_at=datetime(2026, 5, 10, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
                status="active",
            )
            db.add(card)
            db.commit()
            db.refresh(card)
            return str(card.id)

    def get_today(self):
        response = self.client.get(
            "/api/review/today",
            params={
                "user_id": self.user_id,
                "review_date": REVIEW_DATE,
                "timezone": TIMEZONE,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def submit_feedback(self, session, item, card_id, result, client_record_id=None):
        response = self.client.post(
            "/api/review/feedback",
            json={
                "user_id": self.user_id,
                "session_id": session["session_id"],
                "session_item_id": item["id"],
                "card_id": card_id,
                "result": result,
                "reviewed_at": REVIEWED_AT,
                "timezone": TIMEZONE,
                "client_record_id": client_record_id or f"record-{uuid4()}",
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_today_creates_session_from_cards_table(self):
        card = self.create_card_via_api(content="look forward to")

        session = self.get_today()

        self.assertEqual(REVIEW_DATE, session["review_date"])
        self.assertEqual(TIMEZONE, session["timezone"])
        self.assertEqual(1, session["total_count"])
        self.assertEqual(0, session["completed_count"])
        self.assertEqual(0, session["current_index"])
        self.assertEqual(card["id"], session["items"][0]["card_id"])
        self.assertEqual("look forward to", session["items"][0]["card"]["content"])

    def test_today_returns_empty_items_when_no_cards_are_due(self):
        self.create_mastered_future_card()

        session = self.get_today()

        self.assertEqual(0, session["total_count"])
        self.assertEqual([], session["items"])

    def test_today_restores_existing_active_session_without_duplicate_creation(self):
        self.create_card_via_api(content="restore session")

        first = self.get_today()
        second = self.get_today()

        self.assertEqual(first["session_id"], second["session_id"])
        with TestingSessionLocal() as db:
            sessions = list(db.scalars(select(ReviewSession).where(ReviewSession.user_id == self.user_uuid)))
            self.assertEqual(1, len(sessions))

    def test_feedback_saves_review_record(self):
        card = self.create_card_via_api(content="save record")
        session = self.get_today()

        self.submit_feedback(session, session["items"][0], card["id"], "good", "client-record-save")

        with TestingSessionLocal() as db:
            records = list(
                db.scalars(
                    select(ReviewRecord).where(
                        ReviewRecord.user_id == self.user_uuid,
                        ReviewRecord.client_record_id == "client-record-save",
                    )
                )
            )
            self.assertEqual(1, len(records))
            self.assertEqual("good", records[0].result)

    def test_feedback_updates_card_review_count_and_last_result(self):
        card = self.create_card_via_api(content="update card")
        session = self.get_today()

        response = self.submit_feedback(session, session["items"][0], card["id"], "good")

        self.assertEqual(1, response["card_update"]["review_count"])
        self.assertEqual("good", response["card_update"]["last_review_result"])
        with TestingSessionLocal() as db:
            updated = db.get(Card, UUID(card["id"]))
            self.assertEqual(1, updated.review_count)
            self.assertEqual("good", updated.last_review_result)

    def test_again_and_hard_append_repeat_item(self):
        for result in ("again", "hard"):
            with self.subTest(result=result):
                self.reset_database()
                card = self.create_card_via_api(content=f"{result} repeat")
                session = self.get_today()

                response = self.submit_feedback(session, session["items"][0], card["id"], result)

                self.assertFalse(response["completed"])
                self.assertEqual(2, response["session_update"]["total_count"])
                self.assertIsNotNone(response["next_item"])
                self.assertTrue(response["next_item"]["is_repeat"])
                self.assertEqual(1, response["next_item"]["repeat_count"])

    def test_repeat_count_at_limit_does_not_append(self):
        card = self.create_card_via_api(content="repeat limit")
        session = self.get_today()
        first = self.submit_feedback(session, session["items"][0], card["id"], "again", "repeat-1")
        second = self.submit_feedback(
            {"session_id": session["session_id"]},
            first["next_item"],
            card["id"],
            "again",
            "repeat-2",
        )

        third = self.submit_feedback(
            {"session_id": session["session_id"]},
            second["next_item"],
            card["id"],
            "again",
            "repeat-3",
        )

        self.assertTrue(third["completed"])
        self.assertEqual(3, third["session_update"]["total_count"])
        self.assertIsNone(third["next_item"])

    def test_good_and_easy_do_not_append_repeat_item(self):
        for result in ("good", "easy"):
            with self.subTest(result=result):
                self.reset_database()
                card = self.create_card_via_api(content=f"{result} no repeat")
                session = self.get_today()

                response = self.submit_feedback(session, session["items"][0], card["id"], result)

                self.assertTrue(response["completed"])
                self.assertEqual(1, response["session_update"]["total_count"])
                self.assertIsNone(response["next_item"])

    def test_duplicate_client_record_id_does_not_increment_review_count_twice(self):
        card = self.create_card_via_api(content="idempotent")
        session = self.get_today()

        first = self.submit_feedback(session, session["items"][0], card["id"], "good", "same-record")
        second = self.submit_feedback(session, session["items"][0], card["id"], "good", "same-record")

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(1, second["card_update"]["review_count"])
        with TestingSessionLocal() as db:
            updated = db.get(Card, UUID(card["id"]))
            self.assertEqual(1, updated.review_count)

    def test_session_is_completed_when_all_items_are_done(self):
        card = self.create_card_via_api(content="complete session")
        session = self.get_today()

        response = self.submit_feedback(session, session["items"][0], card["id"], "good")

        self.assertTrue(response["completed"])
        self.assertEqual("completed", response["session_update"]["status"])
        self.assertIsNotNone(response["session_update"]["completed_at"])

    def test_summary_returns_result_statistics(self):
        card = self.create_card_via_api(content="summary card")
        session = self.get_today()
        self.submit_feedback(session, session["items"][0], card["id"], "good")

        response = self.client.get(f"/api/review/sessions/{session['session_id']}/summary")

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(session["session_id"], data["session_id"])
        self.assertEqual(1, data["total_count"])
        self.assertEqual(1, data["completed_count"])
        self.assertEqual(0, data["again_count"])
        self.assertEqual(1, data["good_count"])
        self.assertEqual("summary card", data["items"][0]["content"])
        self.assertEqual("good", data["items"][0]["final_result"])
        self.assertEqual(1, data["items"][0]["review_count"])


if __name__ == "__main__":
    unittest.main()
