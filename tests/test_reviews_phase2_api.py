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

    def create_review_log(self, card_id, result, reviewed_at, **overrides):
        user_id = overrides.pop("user_id", self.user_uuid)
        with TestingSessionLocal() as db:
            session = ReviewSession(
                user_id=user_id,
                review_date=reviewed_at.date(),
                timezone="Asia/Shanghai",
                session_type=overrides.pop("session_type", "daily_suggested"),
                started_at=reviewed_at,
                status="completed",
                batch_size=5,
                total_count=1,
                reviewed_count=1,
                completed_count=1,
                planned_new_count=0,
                planned_review_count=1,
                current_index=1,
            )
            db.add(session)
            db.flush()
            item = ReviewSessionItem(
                session_id=session.id,
                card_id=card_id,
                position=0,
                status="reviewed",
                result=result,
                reappear_count=0,
                is_repeat=False,
                repeat_count=0,
                first_result=result,
                final_result=result,
                reviewed_at=reviewed_at,
            )
            db.add(item)
            db.flush()
            log = ReviewLog(
                user_id=user_id,
                card_id=card_id,
                session_id=session.id,
                session_item_id=item.id,
                session_type=session.session_type,
                result=result,
                reviewed_at=reviewed_at,
                card_state_before_review="reviewing",
                review_state_before="reviewing",
                review_state_after="reviewing",
                mastery_score_before=1,
                mastery_score_after=2,
                recovery_stage_before=0,
                recovery_stage_after=0,
            )
            db.add(log)
            db.commit()
            return log.id

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
        self.assertEqual(20, data["extra_today"]["new_only_count"])
        self.assertEqual(2, data["extra_today"]["free_review_count"])
        self.assertEqual(22, data["extra_today"]["total_count"])
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
        self.assertEqual(0, data["extra_today"]["new_only_count"])
        self.assertEqual(0, data["extra_today"]["total_count"])

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

    def test_history_returns_grouped_card_summaries(self):
        card_a = self.create_card(
            content="aggregate alpha",
            content_normalized="aggregate alpha",
            understanding="first meaning",
            note="first note",
            card_type="phrase",
            exam_scene="IELTS",
            exam_module="reading",
        )
        card_b = self.create_card(content="aggregate beta", content_normalized="aggregate beta")
        self.create_review_log(card_a, "forgot", NOW - timedelta(hours=3))
        self.create_review_log(card_b, "shaky", NOW - timedelta(hours=2))
        self.create_review_log(card_a, "got_it", NOW - timedelta(hours=1))

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(2, data["total"])
        self.assertEqual(20, data["limit"])
        self.assertEqual(0, data["offset"])
        self.assertEqual([str(card_a), str(card_b)], [item["card_id"] for item in data["items"]])
        first = data["items"][0]
        self.assertEqual("aggregate alpha", first["content"])
        self.assertEqual("first meaning", first["understanding"])
        self.assertEqual("first note", first["note"])
        self.assertEqual("phrase", first["card_type"])
        self.assertEqual("IELTS", first["exam_scene"])
        self.assertEqual("reading", first["exam_module"])
        self.assertEqual(2, first["review_count_in_range"])
        self.assertEqual("got_it", first["last_result"])
        self.assertEqual("基本掌握", first["last_result_label"])
        self.assertTrue(first["last_reviewed_at"].startswith("2026-05-08T08:30:00"))

    def test_history_aggregation_uses_latest_log_within_card(self):
        card_id = self.create_card(content="latest card", content_normalized="latest card")
        self.create_review_log(card_id, "forgot", NOW - timedelta(hours=4))
        self.create_review_log(card_id, "shaky", NOW - timedelta(hours=2))
        self.create_review_log(card_id, "fluent", NOW)

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        item = response.json()["items"][0]
        self.assertEqual(1, response.json()["total"])
        self.assertEqual(3, item["review_count_in_range"])
        self.assertEqual("fluent", item["last_result"])
        self.assertEqual("很熟了", item["last_result_label"])
        self.assertTrue(item["last_reviewed_at"].startswith("2026-05-08T09:30:00"))

    def test_history_sorts_by_last_reviewed_at_desc(self):
        oldest = self.create_card(content="oldest", content_normalized="oldest")
        newest = self.create_card(content="newest", content_normalized="newest")
        middle = self.create_card(content="middle", content_normalized="middle")
        self.create_review_log(oldest, "forgot", NOW - timedelta(days=3))
        self.create_review_log(newest, "got_it", NOW)
        self.create_review_log(middle, "shaky", NOW - timedelta(days=1))

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            [str(newest), str(middle), str(oldest)],
            [item["card_id"] for item in response.json()["items"]],
        )

    def test_history_result_filter_uses_latest_result_per_card(self):
        changed = self.create_card(content="changed result", content_normalized="changed result")
        still_forgot = self.create_card(content="still forgot", content_normalized="still forgot")
        self.create_review_log(changed, "forgot", NOW - timedelta(hours=2))
        self.create_review_log(changed, "got_it", NOW - timedelta(hours=1))
        self.create_review_log(still_forgot, "forgot", NOW)

        forgot_response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": "forgot"},
        )
        got_it_response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": "got_it"},
        )

        self.assertEqual(200, forgot_response.status_code, forgot_response.text)
        self.assertEqual([str(still_forgot)], [item["card_id"] for item in forgot_response.json()["items"]])
        self.assertEqual(1, forgot_response.json()["total"])
        self.assertEqual(200, got_it_response.status_code, got_it_response.text)
        self.assertEqual([str(changed)], [item["card_id"] for item in got_it_response.json()["items"]])
        self.assertEqual(1, got_it_response.json()["total"])

    def test_history_date_range_counts_only_logs_in_range(self):
        card_id = self.create_card(content="date range", content_normalized="date range")
        self.create_review_log(card_id, "forgot", datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc))
        self.create_review_log(card_id, "shaky", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc))
        self.create_review_log(card_id, "fluent", datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc))

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"date_from": "2026-05-08", "date_to": "2026-05-08"},
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual(1, data["items"][0]["review_count_in_range"])
        self.assertEqual("shaky", data["items"][0]["last_result"])
        self.assertTrue(data["items"][0]["last_reviewed_at"].startswith("2026-05-08T06:00:00"))

    def test_history_limit_offset_apply_to_grouped_cards(self):
        first = self.create_card(content="first page", content_normalized="first page")
        second = self.create_card(content="second page", content_normalized="second page")
        third = self.create_card(content="third page", content_normalized="third page")
        self.create_review_log(first, "forgot", NOW - timedelta(days=2))
        self.create_review_log(first, "got_it", NOW)
        self.create_review_log(second, "shaky", NOW - timedelta(days=1))
        self.create_review_log(third, "fluent", NOW - timedelta(days=3))

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"limit": 1, "offset": 1},
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(3, data["total"])
        self.assertEqual(1, data["limit"])
        self.assertEqual(1, data["offset"])
        self.assertEqual([str(second)], [item["card_id"] for item in data["items"]])

    def test_history_search_matches_card_fields_and_empty_search_result(self):
        content_card = self.create_card(content="alpha phrase", content_normalized="alpha phrase")
        understanding_card = self.create_card(
            content="understanding card",
            content_normalized="understanding card",
            understanding="target meaning",
        )
        note_card = self.create_card(
            content="note card",
            content_normalized="note card",
            note="sticky note",
        )
        scene_card = self.create_card(
            content="scene card",
            content_normalized="scene card",
            exam_scene="IELTS",
        )
        module_card = self.create_card(
            content="module card",
            content_normalized="module card",
            exam_module="reading module",
        )
        for card_id in (content_card, understanding_card, note_card, scene_card, module_card):
            self.create_review_log(card_id, "got_it", NOW)

        cases = [
            ("alpha", content_card),
            ("target meaning", understanding_card),
            ("sticky", note_card),
            ("IELTS", scene_card),
            ("reading module", module_card),
        ]
        for keyword, expected_card in cases:
            with self.subTest(keyword=keyword):
                response = self.client.get(
                    "/api/reviews/history",
                    headers=self.auth_headers(),
                    params={"search": keyword},
                )
                self.assertEqual(200, response.status_code, response.text)
                data = response.json()
                self.assertEqual(1, data["total"])
                self.assertEqual([str(expected_card)], [item["card_id"] for item in data["items"]])

        empty = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"search": "not-found"},
        )
        self.assertEqual(200, empty.status_code, empty.text)
        self.assertEqual(0, empty.json()["total"])
        self.assertEqual([], empty.json()["items"])

    def test_history_empty_state_returns_empty_items(self):
        self.create_card(content="no logs", content_normalized="no logs")

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"items": [], "total": 0, "limit": 20, "offset": 0}, response.json())

    def test_history_is_limited_to_current_user(self):
        own_card = self.create_card(content="own card", content_normalized="own card")
        other_card = self.create_card(
            user_id=self.other_user_uuid,
            content="other card",
            content_normalized="other card",
        )
        self.create_review_log(own_card, "got_it", NOW)
        self.create_review_log(other_card, "forgot", NOW, user_id=self.other_user_uuid)

        own_response = self.client.get("/api/reviews/history", headers=self.auth_headers())
        other_response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(self.other_token),
        )

        self.assertEqual(200, own_response.status_code, own_response.text)
        self.assertEqual([str(own_card)], [item["card_id"] for item in own_response.json()["items"]])
        self.assertEqual(1, own_response.json()["total"])
        self.assertEqual(200, other_response.status_code, other_response.text)
        self.assertEqual([str(other_card)], [item["card_id"] for item in other_response.json()["items"]])
        self.assertEqual(1, other_response.json()["total"])

    def test_history_summary_empty_state_returns_zero_counts(self):
        self.create_card(content="summary no logs", content_normalized="summary no logs")

        response = self.client.get("/api/reviews/history/summary", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "total_reviews": 0,
                "unique_cards": 0,
                "latest_result_card_counts": {
                    "forgot": 0,
                    "shaky": 0,
                    "got_it": 0,
                    "fluent": 0,
                },
                "date_from": None,
                "date_to": None,
            },
            response.json(),
        )

    def test_history_summary_counts_total_reviews_and_unique_cards(self):
        card_a = self.create_card(content="summary repeated", content_normalized="summary repeated")
        card_b = self.create_card(content="summary once", content_normalized="summary once")
        self.create_review_log(card_a, "forgot", NOW - timedelta(hours=3))
        self.create_review_log(card_a, "shaky", NOW - timedelta(hours=2))
        self.create_review_log(card_b, "fluent", NOW - timedelta(hours=1))

        response = self.client.get("/api/reviews/history/summary", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(3, data["total_reviews"])
        self.assertEqual(2, data["unique_cards"])
        self.assertEqual(
            {"forgot": 0, "shaky": 1, "got_it": 0, "fluent": 1},
            data["latest_result_card_counts"],
        )

    def test_history_summary_latest_result_counts_use_latest_log_per_card(self):
        card_a = self.create_card(content="summary card a", content_normalized="summary card a")
        card_b = self.create_card(content="summary card b", content_normalized="summary card b")
        card_c = self.create_card(content="summary card c", content_normalized="summary card c")
        self.create_review_log(card_a, "forgot", NOW - timedelta(hours=5))
        self.create_review_log(card_a, "shaky", NOW - timedelta(hours=4))
        self.create_review_log(card_a, "got_it", NOW - timedelta(hours=3))
        self.create_review_log(card_b, "forgot", NOW - timedelta(hours=2))
        self.create_review_log(card_c, "fluent", NOW - timedelta(hours=1))

        response = self.client.get("/api/reviews/history/summary", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(5, data["total_reviews"])
        self.assertEqual(3, data["unique_cards"])
        self.assertEqual(
            {"forgot": 1, "shaky": 0, "got_it": 1, "fluent": 1},
            data["latest_result_card_counts"],
        )

    def test_history_summary_date_range_uses_latest_log_inside_range(self):
        card_a = self.create_card(content="summary date a", content_normalized="summary date a")
        card_b = self.create_card(content="summary date b", content_normalized="summary date b")
        self.create_review_log(card_a, "forgot", datetime(2026, 5, 7, 5, 0, tzinfo=timezone.utc))
        self.create_review_log(card_a, "shaky", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc))
        self.create_review_log(card_a, "fluent", datetime(2026, 5, 9, 2, 0, tzinfo=timezone.utc))
        self.create_review_log(card_b, "got_it", datetime(2026, 5, 9, 3, 0, tzinfo=timezone.utc))

        response = self.client.get(
            "/api/reviews/history/summary",
            headers=self.auth_headers(),
            params={"date_from": "2026-05-08", "date_to": "2026-05-08"},
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total_reviews"])
        self.assertEqual(1, data["unique_cards"])
        self.assertEqual(
            {"forgot": 0, "shaky": 1, "got_it": 0, "fluent": 0},
            data["latest_result_card_counts"],
        )
        self.assertEqual("2026-05-08", data["date_from"])
        self.assertEqual("2026-05-08", data["date_to"])

    def test_history_summary_rejects_invalid_date_range(self):
        response = self.client.get(
            "/api/reviews/history/summary",
            headers=self.auth_headers(),
            params={"date_from": "2026-05-09", "date_to": "2026-05-08"},
        )

        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(
            "date_from must be earlier than or equal to date_to",
            response.json()["detail"],
        )

    def test_history_summary_is_limited_to_current_user(self):
        own_card = self.create_card(content="summary own", content_normalized="summary own")
        other_card = self.create_card(
            user_id=self.other_user_uuid,
            content="summary other",
            content_normalized="summary other",
        )
        self.create_review_log(own_card, "got_it", NOW - timedelta(hours=2))
        self.create_review_log(own_card, "fluent", NOW - timedelta(hours=1))
        self.create_review_log(other_card, "forgot", NOW, user_id=self.other_user_uuid)

        own_response = self.client.get("/api/reviews/history/summary", headers=self.auth_headers())
        other_response = self.client.get(
            "/api/reviews/history/summary",
            headers=self.auth_headers(self.other_token),
        )

        self.assertEqual(200, own_response.status_code, own_response.text)
        self.assertEqual(2, own_response.json()["total_reviews"])
        self.assertEqual(1, own_response.json()["unique_cards"])
        self.assertEqual(
            {"forgot": 0, "shaky": 0, "got_it": 0, "fluent": 1},
            own_response.json()["latest_result_card_counts"],
        )
        self.assertEqual(200, other_response.status_code, other_response.text)
        self.assertEqual(1, other_response.json()["total_reviews"])
        self.assertEqual(1, other_response.json()["unique_cards"])
        self.assertEqual(
            {"forgot": 1, "shaky": 0, "got_it": 0, "fluent": 0},
            other_response.json()["latest_result_card_counts"],
        )

    def test_history_result_single_value_still_compatible(self):
        card_a = self.create_card(content="compat a", content_normalized="compat a")
        card_b = self.create_card(content="compat b", content_normalized="compat b")
        self.create_review_log(card_a, "forgot", NOW - timedelta(hours=2))
        self.create_review_log(card_b, "got_it", NOW - timedelta(hours=1))

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": "forgot"},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual(str(card_a), data["items"][0]["card_id"])

    def test_history_result_multi_value_got_it_fluent(self):
        card_a = self.create_card(content="multi a", content_normalized="multi a")
        card_b = self.create_card(content="multi b", content_normalized="multi b")
        card_c = self.create_card(content="multi c", content_normalized="multi c")
        self.create_review_log(card_a, "forgot", NOW - timedelta(hours=3))
        self.create_review_log(card_b, "got_it", NOW - timedelta(hours=2))
        self.create_review_log(card_c, "fluent", NOW - timedelta(hours=1))

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": ["got_it", "fluent"]},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(2, data["total"])
        returned_ids = [item["card_id"] for item in data["items"]]
        self.assertIn(str(card_b), returned_ids)
        self.assertIn(str(card_c), returned_ids)

    def test_history_result_multi_value_with_search(self):
        card_a = self.create_card(content="apple pie", content_normalized="apple pie")
        card_b = self.create_card(content="apple juice", content_normalized="apple juice")
        card_c = self.create_card(content="banana bread", content_normalized="banana bread")
        self.create_review_log(card_a, "got_it", NOW - timedelta(hours=3))
        self.create_review_log(card_b, "fluent", NOW - timedelta(hours=2))
        self.create_review_log(card_c, "got_it", NOW - timedelta(hours=1))

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": ["got_it", "fluent"], "search": "apple"},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(2, data["total"])
        returned_ids = [item["card_id"] for item in data["items"]]
        self.assertIn(str(card_a), returned_ids)
        self.assertIn(str(card_b), returned_ids)

    def test_history_result_multi_value_with_limit_offset(self):
        cards = []
        for i in range(6):
            card_id = self.create_card(
                content=f"multi page {i}", content_normalized=f"multi page {i}"
            )
            cards.append(card_id)
            self.create_review_log(card_id, "forgot", NOW - timedelta(hours=i))
        cards.reverse()

        response = self.client.get(
            "/api/reviews/history",
            headers=self.auth_headers(),
            params={"result": "forgot", "limit": 2, "offset": 1},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(6, data["total"])
        self.assertEqual(2, len(data["items"]))
        returned_ids = [item["card_id"] for item in data["items"]]
        self.assertNotIn(str(cards[0]), returned_ids)

    def test_history_summary_search_narrows_counts(self):
        card_a = self.create_card(content="alpha", content_normalized="alpha", understanding="first")
        card_b = self.create_card(content="beta", content_normalized="beta")
        self.create_review_log(card_a, "got_it", NOW - timedelta(hours=2))
        self.create_review_log(card_b, "fluent", NOW - timedelta(hours=1))

        response = self.client.get(
            "/api/reviews/history/summary",
            headers=self.auth_headers(),
            params={"search": "alpha"},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total_reviews"])
        self.assertEqual(1, data["unique_cards"])
        self.assertEqual(1, data["latest_result_card_counts"]["got_it"])

    def test_history_summary_search_empty_result(self):
        card = self.create_card(content="present", content_normalized="present")
        self.create_review_log(card, "fluent", NOW)

        response = self.client.get(
            "/api/reviews/history/summary",
            headers=self.auth_headers(),
            params={"search": "not-found"},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(0, data["total_reviews"])
        self.assertEqual(0, data["unique_cards"])
        self.assertEqual(
            {"forgot": 0, "shaky": 0, "got_it": 0, "fluent": 0},
            data["latest_result_card_counts"],
        )

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

    # ===== Phase 5-5B: review_log_id in history list =====

    def test_history_list_includes_review_log_id(self):
        """Phase 5-5B: each history list item must carry the real ReviewLog.id."""
        card_id = self.create_card(
            content="log id card",
            content_normalized="log id card",
        )
        log_id_1 = self.create_review_log(card_id, "forgot", NOW - timedelta(hours=2))
        log_id_2 = self.create_review_log(card_id, "got_it", NOW - timedelta(hours=1))

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total"])
        item = data["items"][0]
        self.assertIn("review_log_id", item)
        self.assertEqual(str(log_id_2), item["review_log_id"])
        self.assertIn("card_id", item)
        self.assertEqual(str(card_id), item["card_id"])

    def test_history_list_multiple_cards_each_has_review_log_id(self):
        """Phase 5-5B: multiple cards each carry their latest review_log_id."""
        card_a = self.create_card(content="multi log a", content_normalized="multi log a")
        card_b = self.create_card(content="multi log b", content_normalized="multi log b")
        log_a = self.create_review_log(card_a, "shaky", NOW - timedelta(hours=3))
        log_b = self.create_review_log(card_b, "fluent", NOW - timedelta(hours=1))
        # card_a has a newer log
        log_a2 = self.create_review_log(card_a, "got_it", NOW - timedelta(hours=2))

        response = self.client.get("/api/reviews/history", headers=self.auth_headers())

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(2, data["total"])
        items_by_card = {item["card_id"]: item for item in data["items"]}
        self.assertEqual(str(log_a2), items_by_card[str(card_a)]["review_log_id"])
        self.assertEqual(str(log_b), items_by_card[str(card_b)]["review_log_id"])

    # ===== Phase 5-5B: review history detail endpoint =====

    def test_history_detail_returns_full_log_data(self):
        """Phase 5-5B: GET /api/reviews/history/{log_id} returns 200 with log + card."""
        card_id = self.create_card(
            content="detail card",
            content_normalized="detail card",
            understanding="我的理解",
            note="备注",
            card_type="sentence",
            exam_scene="IELTS",
            exam_module="speaking",
            review_state="reviewing",
        )
        log_id = self.create_review_log(
            card_id, "got_it", NOW,
            session_type="daily_suggested",
        )

        response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()

        self.assertEqual(str(log_id), data["id"])
        self.assertEqual(str(log_id), data["review_log_id"])
        self.assertEqual("got_it", data["result"])
        self.assertEqual("基本掌握", data["result_label"])
        self.assertEqual("daily_suggested", data["session_type"])
        self.assertEqual("系统今日推荐", data["session_type_label"])
        self.assertIsNotNone(data["reviewed_at"])

        self.assertIsNotNone(data["card"])
        card = data["card"]
        self.assertEqual(str(card_id), card["id"])
        self.assertEqual(str(card_id), card["card_id"])
        self.assertEqual("detail card", card["content"])
        self.assertEqual("我的理解", card["understanding"])
        self.assertEqual("备注", card["note"])
        self.assertEqual("sentence", card["card_type"])
        self.assertEqual("IELTS", card["exam_scene"])
        self.assertEqual("speaking", card["exam_module"])
        self.assertEqual("reviewing", card["review_state"])

    def test_history_detail_session_type_labels(self):
        """Phase 5-5B: session_type_label maps for all known types."""
        card_id = self.create_card(content="type card", content_normalized="type card")

        cases = [
            ("daily_suggested", "系统今日推荐"),
            ("new_only", "主动新学"),
            ("free_review", "自由复习"),
        ]
        for session_type, expected_label in cases:
            with self.subTest(session_type=session_type):
                log_id = self.create_review_log(
                    card_id, "fluent", NOW - timedelta(hours=1),
                    session_type=session_type,
                )
                response = self.client.get(
                    f"/api/reviews/history/{log_id}",
                    headers=self.auth_headers(),
                )
                self.assertEqual(200, response.status_code, response.text)
                data = response.json()
                self.assertEqual(session_type, data["session_type"])
                self.assertEqual(expected_label, data["session_type_label"])

    def test_history_detail_unknown_session_type_fallback_label(self):
        """Phase 5-5B: unknown session_type falls back to 其他来源."""
        card_id = self.create_card(content="unknown type", content_normalized="unknown type")
        with TestingSessionLocal() as db:
            session = ReviewSession(
                user_id=self.user_uuid,
                review_date=NOW.date(),
                timezone="Asia/Shanghai",
                session_type="daily_suggested",
                started_at=NOW,
                status="completed",
                batch_size=5,
                total_count=1,
                reviewed_count=1,
                completed_count=1,
            )
            db.add(session)
            db.flush()
            item = ReviewSessionItem(
                session_id=session.id,
                card_id=card_id,
                position=0,
                status="reviewed",
                result="fluent",
                reappear_count=0,
                is_repeat=False,
                repeat_count=0,
                first_result="fluent",
                final_result="fluent",
                reviewed_at=NOW,
            )
            db.add(item)
            db.flush()
            log = ReviewLog(
                user_id=self.user_uuid,
                card_id=card_id,
                session_id=session.id,
                session_item_id=item.id,
                session_type="ancient_ritual",
                result="fluent",
                reviewed_at=NOW,
                card_state_before_review="reviewing",
                review_state_before="reviewing",
                review_state_after="reviewing",
                mastery_score_before=1,
                mastery_score_after=2,
                recovery_stage_before=0,
                recovery_stage_after=0,
            )
            db.add(log)
            db.commit()
            log_id = log.id

        response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("其他来源", response.json()["session_type_label"])

    def test_history_detail_log_not_found_returns_404(self):
        """Phase 5-5B: non-existent log_id returns 404."""
        non_existent_id = uuid4()

        response = self.client.get(
            f"/api/reviews/history/{non_existent_id}",
            headers=self.auth_headers(),
        )

        self.assertEqual(404, response.status_code, response.text)

    def test_history_detail_card_soft_deleted_returns_card_null(self):
        """Phase 5-5B: log exists but card is soft-deleted → 200 + card:null."""
        card_id = self.create_card(
            content="deleted card",
            content_normalized="deleted card",
        )
        log_id = self.create_review_log(card_id, "shaky", NOW)

        # Soft-delete the card
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.deleted_at = NOW
            card.status = "deleted"
            db.commit()

        response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(str(log_id), data["id"])
        self.assertEqual("shaky", data["result"])
        self.assertIsNone(data["card"])

    def test_history_detail_card_archived_returns_card_null(self):
        """Phase 5-5B: log exists but card is archived → 200 + card:null."""
        card_id = self.create_card(
            content="archived card",
            content_normalized="archived card",
        )
        log_id = self.create_review_log(card_id, "fluent", NOW)

        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.status = "archived"
            db.commit()

        response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )

        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(str(log_id), data["id"])
        self.assertIsNone(data["card"])

    def test_history_detail_user_isolation(self):
        """Phase 5-5B: user A's log returns 404 when requested by user B."""
        card_id = self.create_card(
            content="isolation card",
            content_normalized="isolation card",
        )
        log_id = self.create_review_log(card_id, "got_it", NOW)

        # User B tries to access User A's log
        other_response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(self.other_token),
        )

        self.assertEqual(404, other_response.status_code, other_response.text)

        # User A can still access their own log
        own_response = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, own_response.status_code, own_response.text)

    def test_history_detail_result_label_for_all_results(self):
        """Phase 5-5B: result_label uses REVIEW_RESULT_LABELS for all values."""
        card_id = self.create_card(
            content="result label card",
            content_normalized="result label card",
        )

        expected_labels = {
            "forgot": "想不起来",
            "shaky": "不太稳",
            "got_it": "基本掌握",
            "fluent": "很熟了",
        }
        for result, expected_label in expected_labels.items():
            with self.subTest(result=result):
                log_id = self.create_review_log(
                    card_id, result, NOW - timedelta(hours=1),
                )
                response = self.client.get(
                    f"/api/reviews/history/{log_id}",
                    headers=self.auth_headers(),
                )
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(expected_label, response.json()["result_label"])


if __name__ == "__main__":
    unittest.main()
