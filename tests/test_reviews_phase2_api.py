from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
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
        # Phase 6L-hotfix-4: suggested.new_count 不再按 new-quota 裁剪，
        # 改为上报全部可学新卡数（与 select_review_cards 填充 slot 后一致）。
        # total_count 上限为 session limit (5)。
        self.assertEqual(20, data["suggested"]["new_count"])
        self.assertEqual(1, data["suggested"]["strengthening_count"])
        self.assertEqual(1, data["suggested"]["due_count"])
        self.assertEqual(5, data["suggested"]["total_count"])
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
        # Phase 6G: failed card with content — allowed into review
        self.create_card(
            content="failed has content",
            content_normalized="failed has content",
            understanding=None,
            analysis_status="failed",
            is_review_ready=True,
            needs_manual_fix=False,
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
        self.assertIn("ready done", contents)
        self.assertIn("pending card", contents)
        self.assertIn("failed has content", contents)
        self.assertIn("failed ready", contents)

    # ========== Phase 6G: understanding/translation empty does not block review ==========

    def test_phase6g_card_without_understanding_can_enter_review(self):
        """Card with content but understanding=None can enter review session."""
        self.create_card(
            content="no understanding",
            content_normalized="no understanding",
            understanding=None,
            is_review_ready=True,
            needs_manual_fix=False,
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("no understanding", items[0]["content"])

    def test_phase6g_card_without_understanding_and_translation_can_enter_review(self):
        """Card with content but both understanding=None and translation=None can enter review."""
        self.create_card(
            content="bare content",
            content_normalized="bare content",
            understanding=None,
            translation=None,
            is_review_ready=True,
            needs_manual_fix=False,
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("bare content", items[0]["content"])

    def test_phase6g_empty_content_card_still_blocked(self):
        """Card with empty content cannot enter review."""
        self.create_card(
            content="",
            content_normalized="",
            understanding="meaning",
            is_review_ready=False,
            needs_manual_fix=False,
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertIsNone(data["session_id"])
        self.assertEqual([], data["items"])

    def test_phase6g_deleted_card_still_blocked(self):
        """Deleted cards cannot enter review."""
        card_id = self.create_card(
            content="deleted card",
            content_normalized="deleted card",
            is_review_ready=True,
            needs_manual_fix=False,
        )
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.deleted_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
            card.status = "deleted"
            db.commit()

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertIsNone(data["session_id"])
        self.assertEqual([], data["items"])

    def test_phase6g_all_session_types_allow_no_understanding_cards(self):
        """daily_suggested, new_only, free_review all allow cards without understanding."""
        self.create_card(
            content="suggested card",
            content_normalized="suggested card",
            understanding=None,
            is_review_ready=True,
            needs_manual_fix=False,
        )
        # Create an older card for free_review
        self.create_card(
            content="old card",
            content_normalized="old card",
            understanding=None,
            review_state="reviewing",
            next_review_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            is_review_ready=True,
            needs_manual_fix=False,
        )

        for session_type in ("daily_suggested", "new_only", "free_review"):
            with self.subTest(session_type=session_type):
                response = self.client.post(
                    "/api/review-sessions",
                    headers=self.auth_headers(),
                    json={"session_type": session_type, "limit": 5, "restart": True},
                )
                self.assertEqual(200, response.status_code, response.text)

    def test_phase6g_normal_card_with_understanding_still_works(self):
        """Regression: cards with understanding still enter review normally."""
        self.create_card(
            content="normal card",
            content_normalized="normal card",
            understanding="the meaning",
            is_review_ready=True,
            needs_manual_fix=False,
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("normal card", items[0]["content"])

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
        self.create_card(
            content="card a",
            content_normalized="card a",
            created_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
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

    # ===== Phase 6B-1: ClientAction zombie processing tests =====

    def test_processing_non_zombie_returns_409(self):
        """A: processing not yet zombie → 409, no duplicate ReviewLog, no Card change."""
        card_id = self.create_card(content="non-zombie", content_normalized="non-zombie")
        today, item = self._start_session()
        client_action_id = str(uuid4())

        # Manually insert a fresh processing ClientAction
        with TestingSessionLocal() as db:
            action = ClientAction(
                user_id=self.user_uuid,
                client_action_id=client_action_id,
                action_type="review_feedback",
                status="processing",
            )
            db.add(action)
            db.commit()

        # Submit with same client_action_id — should get 409
        resp = self.client.post(
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
        self.assertEqual(409, resp.status_code, resp.text)
        self.assertIn("being processed", resp.json()["detail"])

        # No ReviewLog should have been written
        with TestingSessionLocal() as db:
            logs = list(db.scalars(select(ReviewLog).where(ReviewLog.card_id == card_id)))
            self.assertEqual(0, len(logs))
            # Card review_state unchanged
            card = db.get(Card, card_id)
            self.assertEqual("new", card.review_state)

    def test_processing_zombie_allows_reprocessing(self):
        """B: processing zombie (>5 min) → re-processing allowed, one ReviewLog written."""
        card_id = self.create_card(content="zombie", content_normalized="zombie")
        today, item = self._start_session()
        client_action_id = str(uuid4())

        # Manually insert a zombie processing ClientAction (10 min ago)
        ten_min_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
        with TestingSessionLocal() as db:
            action = ClientAction(
                user_id=self.user_uuid,
                client_action_id=client_action_id,
                action_type="review_feedback",
                status="processing",
                updated_at=ten_min_ago,
            )
            db.add(action)
            db.commit()

        # Submit with same client_action_id — zombie should be re-armed
        resp = self.client.post(
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
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual("success", resp.json()["status"])

        # One ReviewLog should exist
        with TestingSessionLocal() as db:
            logs = list(db.scalars(select(ReviewLog).where(ReviewLog.card_id == card_id)))
            self.assertEqual(1, len(logs))
            self.assertEqual("forgot", logs[0].result)
            # ClientAction → succeeded (not a duplicate row)
            actions = list(db.scalars(
                select(ClientAction).where(ClientAction.client_action_id == client_action_id)
            ))
            self.assertEqual(1, len(actions))
            self.assertEqual("succeeded", actions[0].status)
            # Card updated
            card = db.get(Card, card_id)
            self.assertEqual("strengthening", card.review_state)

    def test_succeeded_no_duplicate_review_log(self):
        """C: same client_action_id after success → no second ReviewLog, Card unchanged."""
        card_id = self.create_card(
            content="idempotent",
            content_normalized="idempotent",
            created_at=NOW - timedelta(days=1),
        )
        today, item = self._start_session()
        client_action_id = str(uuid4())

        # First submission — success
        first = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "fluent",
            },
        )
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("success", first.json()["status"])

        # Capture Card state after first success
        with TestingSessionLocal() as db:
            card_after_first = db.get(Card, card_id)
            state_after_first = card_after_first.review_state
            review_count_after_first = card_after_first.review_count
            log_count_after_first = db.scalar(
                select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
            )

        # Second submission with same client_action_id
        second = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual("success", second.json()["status"])

        # Verify: no duplicate ReviewLog, Card state unchanged
        with TestingSessionLocal() as db:
            log_count_after_second = db.scalar(
                select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card_id)
            )
            self.assertEqual(log_count_after_first, log_count_after_second)
            card_after_second = db.get(Card, card_id)
            self.assertEqual(state_after_first, card_after_second.review_state)
            self.assertEqual(review_count_after_first, card_after_second.review_count)

    def test_card_snapshot_written_on_feedback(self):
        """A: submit_review_feedback writes card_snapshot with card content fields."""
        card_id = self.create_card(
            content="snapshot test content",
            content_normalized="snapshot test content",
            translation="快照参考释义",
            understanding="my understanding",
            note="my note",
            card_type="sentence",
            exam_scene="exam scene",
            exam_module="exam module",
            source_context="The full sentence that originally contained the expression.",
            source_url="https://example.com/source?t=30",
            example_sentence="This is a structured example sentence.",
            example_translation="这是一个结构化例句。",
            analysis_status="done",
            analysis_level="pass",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]
        self.assertEqual("快照参考释义", item["translation"])

        client_action_id = str(uuid4())
        resp = self.client.post(
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
        self.assertEqual(200, resp.status_code, resp.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            self.assertIsNotNone(log)
            self.assertIsNotNone(log.card_snapshot)
            s = log.card_snapshot
            self.assertEqual("snapshot test content", s.get("content"))
            self.assertEqual("快照参考释义", s.get("translation"))
            self.assertEqual("my understanding", s.get("understanding"))
            self.assertEqual("my note", s.get("note"))
            self.assertEqual("sentence", s.get("card_type"))
            self.assertEqual("exam scene", s.get("exam_scene"))
            self.assertEqual("exam module", s.get("exam_module"))
            self.assertEqual(
                "The full sentence that originally contained the expression.",
                s.get("source_context"),
            )
            self.assertEqual("https://example.com/source?t=30", s.get("source_url"))
            self.assertEqual(
                "This is a structured example sentence.",
                s.get("example_sentence"),
            )
            self.assertEqual("这是一个结构化例句。", s.get("example_translation"))
            self.assertEqual("done", s.get("analysis_status"))
            self.assertEqual("pass", s.get("analysis_level"))
            self.assertEqual(str(card_id), s.get("card_id"))
            # Scheduling fields NOT in snapshot
            self.assertNotIn("review_state", s)
            self.assertNotIn("next_review_at", s)

    def test_history_list_returns_snapshot_after_card_edit(self):
        """B: After card is edited, history list returns snapshot content."""
        card_id = self.create_card(
            content="original content",
            content_normalized="original content",
            translation="original translation",
            understanding="original understanding",
            note="original note",
            card_type="word",
            exam_scene="original scene",
            exam_module="original module",
            source_context="original source context",
            source_url="https://example.com/original",
            example_sentence="original example sentence",
            example_translation="原始例句翻译",
            analysis_status="done",
            analysis_level="pass",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        client_action_id = str(uuid4())
        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        # Edit the card
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.content = "edited content"
            card.translation = "edited translation"
            card.understanding = "edited understanding"
            card.note = "edited note"
            card.card_type = "phrase"
            card.exam_scene = "edited scene"
            card.exam_module = "edited module"
            card.source_context = "edited source context"
            card.source_url = "https://example.com/edited"
            card.example_sentence = "edited example sentence"
            card.example_translation = "编辑后的例句翻译"
            db.commit()

        # Query history list
        hist = self.client.get(
            "/api/reviews/history?limit=20",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, hist.status_code, hist.text)
        data = hist.json()
        self.assertGreater(len(data["items"]), 0)
        found = [i for i in data["items"] if i["card_id"] == str(card_id)]
        self.assertEqual(1, len(found))
        item_data = found[0]
        self.assertEqual("original content", item_data["content"])
        self.assertEqual("original translation", item_data["translation"])
        self.assertEqual("original understanding", item_data["understanding"])
        self.assertEqual("original note", item_data["note"])
        self.assertEqual("word", item_data["card_type"])
        self.assertEqual("original scene", item_data["exam_scene"])
        self.assertEqual("original module", item_data["exam_module"])
        self.assertEqual("original source context", item_data["source_context"])
        self.assertEqual("https://example.com/original", item_data["source_url"])
        self.assertEqual("original example sentence", item_data["example_sentence"])
        self.assertEqual("原始例句翻译", item_data["example_translation"])
        self.assertEqual("snapshot", item_data["card_source"])

    def test_history_detail_returns_snapshot_after_card_edit(self):
        """C: After card is edited, history detail returns snapshot content."""
        card_id = self.create_card(
            content="detail original",
            content_normalized="detail original",
            translation="detail original translation",
            understanding="detail understanding",
            note="detail note",
            card_type="word",
            exam_scene="detail scene",
            exam_module="detail module",
            source_context="detail source context",
            source_url="https://example.com/detail",
            example_sentence="detail example sentence",
            example_translation="详情例句翻译",
            analysis_status="done",
            analysis_level="pass",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        client_action_id = str(uuid4())
        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "shaky",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        # Get log ID
        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            log_id = str(log.id)

        # Edit the card
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.content = "edited detail"
            card.translation = "edited detail translation"
            card.understanding = "edited understanding"
            card.note = "edited note"
            card.source_context = "edited detail source context"
            card.source_url = "https://example.com/detail-edited"
            card.example_sentence = "edited detail example sentence"
            card.example_translation = "编辑后的详情例句翻译"
            db.commit()

        # Query history detail
        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        data = detail.json()
        self.assertIsNotNone(data["card"])
        self.assertEqual("detail original", data["card"]["content"])
        self.assertEqual("detail original translation", data["card"]["translation"])
        self.assertEqual("detail understanding", data["card"]["understanding"])
        self.assertEqual("detail note", data["card"]["note"])
        self.assertEqual("detail source context", data["card"]["source_context"])
        self.assertEqual("https://example.com/detail", data["card"]["source_url"])
        self.assertEqual("detail example sentence", data["card"]["example_sentence"])
        self.assertEqual("详情例句翻译", data["card"]["example_translation"])
        self.assertEqual("snapshot", data["card"]["card_source"])
        # Scheduling fields should be null for snapshot
        self.assertIsNone(data["card"]["review_state"])
        self.assertIsNone(data["card"]["next_review_at"])

    def test_old_log_without_snapshot_falls_back_to_card(self):
        """D: Old log without card_snapshot returns current Card data."""
        card_id = self.create_card(
            content="current content",
            content_normalized="current content",
            understanding="current understanding",
            note="current note",
            card_type="word",
        )
        reviewed_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        log_id = self.create_review_log(card_id=card_id, result="forgot", reviewed_at=reviewed_at)

        # Verify log has no snapshot
        with TestingSessionLocal() as db:
            log = db.get(ReviewLog, log_id)
            self.assertIsNone(log.card_snapshot)

        # History list
        hist = self.client.get(
            "/api/reviews/history?limit=20",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, hist.status_code, hist.text)
        data = hist.json()
        found = [i for i in data["items"] if i["card_id"] == str(card_id)]
        self.assertEqual(1, len(found))
        self.assertEqual("current content", found[0]["content"])
        self.assertEqual("current understanding", found[0]["understanding"])
        self.assertEqual("current_card", found[0]["card_source"])

        # History detail
        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertEqual("current content", d["card"]["content"])
        self.assertEqual("current_card", d["card"]["card_source"])

    def test_deleted_card_with_snapshot_still_displays(self):
        """E: Deleted card + snapshot → card data from snapshot, card_source=snapshot."""
        card_id = self.create_card(
            content="deleted snapshot content",
            content_normalized="deleted snapshot content",
            understanding="survives deletion",
            note="persistent note",
            card_type="sentence",
            exam_scene="deleted scene",
            exam_module="deleted module",
            analysis_status="done",
            analysis_level="pass",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        client_action_id = str(uuid4())
        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": client_action_id,
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "fluent",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            log_id = str(log.id)

        # Soft-delete the card
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.deleted_at = datetime.now(timezone.utc)
            db.commit()

        # History detail should still return card from snapshot
        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertEqual("deleted snapshot content", d["card"]["content"])
        self.assertEqual("survives deletion", d["card"]["understanding"])
        self.assertEqual("snapshot", d["card"]["card_source"])

    def test_snapshot_includes_where_encountered(self):
        """_build_card_snapshot captures where_encountered at review time."""
        card_id = self.create_card(
            content="clutch",
            content_normalized="clutch",
            where_encountered="NBA解说",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            self.assertIsNotNone(log)
            self.assertIsNotNone(log.card_snapshot)
            self.assertEqual("NBA解说", log.card_snapshot.get("where_encountered"))

    def test_history_detail_snapshot_returns_where_encountered(self):
        """GET /api/reviews/history/{log_id} returns snapshot where_encountered."""
        card_id = self.create_card(
            content="break a leg",
            content_normalized="break a leg",
            where_encountered="美剧Friends",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "fluent",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            log_id = str(log.id)

        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertEqual("snapshot", d["card"]["card_source"])
        self.assertEqual("美剧Friends", d["card"]["where_encountered"])

    def test_history_detail_snapshot_where_encountered_unchanged_after_card_edit(self):
        """Editing Card.where_encountered must not change old history detail snapshot."""
        card_id = self.create_card(
            content="original",
            content_normalized="original",
            where_encountered="old source",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            log_id = str(log.id)

        # Edit the card's where_encountered after review
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.where_encountered = "new edited source"
            db.commit()

        # History detail must still return old snapshot value
        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertEqual("snapshot", d["card"]["card_source"])
        self.assertEqual("old source", d["card"]["where_encountered"])

    def test_history_detail_old_snapshot_without_where_encountered_returns_null(self):
        """Old snapshot without where_encountered key returns null, not error."""
        card_id = self.create_card(
            content="no source",
            content_normalized="no source",
            source_context="current context must not leak into an old snapshot",
            source_url="https://example.com/current",
            example_sentence="Current example must not leak.",
            example_translation="当前例句不能泄漏到旧快照。",
        )
        now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

        # Directly insert a ReviewLog with old-style snapshot (no where_encountered)
        with TestingSessionLocal() as db:
            session = ReviewSession(
                user_id=self.user_uuid,
                review_date=now.date(),
                timezone="Asia/Shanghai",
                session_type="daily_suggested",
                started_at=now,
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
                result="got_it",
            )
            db.add(item)
            db.flush()

            old_snapshot = {
                "card_id": str(card_id),
                "content": "no source",
                "understanding": None,
                "note": None,
                "card_type": "word",
                "exam_scene": None,
                "exam_module": None,
                "analysis_status": "done",
                "analysis_level": "pass",
            }
            log = ReviewLog(
                user_id=self.user_uuid,
                card_id=card_id,
                session_id=session.id,
                session_item_id=item.id,
                session_type="daily_suggested",
                result="got_it",
                reviewed_at=now,
                card_snapshot=old_snapshot,
                card_state_before_review="new",
                review_state_before="new",
                review_state_after="reviewing",
                mastery_score_before=0,
                mastery_score_after=1,
                recovery_stage_before=0,
                recovery_stage_after=0,
            )
            db.add(log)
            db.commit()
            log_id = str(log.id)

        # History detail must return null for where_encountered, not crash
        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertIsNone(d["card"]["where_encountered"])
        self.assertIsNone(d["card"]["source_context"])
        self.assertIsNone(d["card"]["source_url"])
        self.assertIsNone(d["card"]["example_sentence"])
        self.assertIsNone(d["card"]["example_translation"])

        history = self.client.get(
            "/api/reviews/history?limit=20",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, history.status_code, history.text)
        history_item = next(
            item for item in history.json()["items"] if item["card_id"] == str(card_id)
        )
        self.assertIsNone(history_item["source_context"])
        self.assertIsNone(history_item["source_url"])
        self.assertIsNone(history_item["example_sentence"])
        self.assertIsNone(history_item["example_translation"])

    def test_history_detail_card_where_encountered_null_when_not_set(self):
        """History detail returns null when Card.where_encountered is not set."""
        card_id = self.create_card(
            content="plain card",
            content_normalized="plain card",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
            log_id = str(log.id)

        detail = self.client.get(
            f"/api/reviews/history/{log_id}",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, detail.status_code, detail.text)
        d = detail.json()
        self.assertIsNotNone(d["card"])
        self.assertIsNone(d["card"]["where_encountered"])

    def test_history_list_search_by_where_encountered(self):
        """GET /api/reviews/history?search=<keyword> finds cards by where_encountered."""
        card_id = self.create_card(
            content="clutch",
            content_normalized="clutch",
            where_encountered="NBA解说",
        )
        today = self.client.get(
            "/api/reviews/today?limit=5&restart=true", headers=self.auth_headers()
        ).json()
        item = today["items"][0]

        fb = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": today["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )
        self.assertEqual(200, fb.status_code, fb.text)

        # Search by NBA should find the card
        response = self.client.get(
            "/api/reviews/history?search=NBA",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual("NBA解说", data["items"][0]["where_encountered"])

        # Search by unrelated keyword returns empty
        response2 = self.client.get(
            "/api/reviews/history?search=xyznotfound",
            headers=self.auth_headers(),
        )
        self.assertEqual(200, response2.status_code, response2.text)
        self.assertEqual(0, response2.json()["total"])

    def test_overview_deleted_cards_not_counted_in_suggested(self):
        """After deleting all cards, suggested.total_count must be 0."""
        card1 = self.create_card(content="card one", content_normalized="card one")
        card2 = self.create_card(content="card two", content_normalized="card two")

        # Verify 2 cards visible
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        self.assertEqual(2, overview.json()["suggested"]["total_count"])

        # Soft-delete both cards
        with TestingSessionLocal() as db:
            for cid in (card1, card2):
                card = db.get(Card, cid)
                card.status = "deleted"
                card.deleted_at = datetime.now(timezone.utc)
            db.commit()

        # After deletion, suggested.total_count must be 0
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        self.assertEqual(0, overview.json()["suggested"]["total_count"])
        self.assertTrue(overview.json()["is_all_done"])

    def test_overview_completed_excludes_deleted_card_review_logs(self):
        """completed_suggested must not count ReviewLogs of deleted cards."""
        card_id = self.create_card(content="keep me", content_normalized="keep me")

        # Review via daily_suggested API (actual today's date)
        daily = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5},
        ).json()
        self.assertIsNotNone(daily.get("session_id"))
        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": daily["session_id"],
                "session_item_id": daily["items"][0]["session_item_id"],
                "card_id": str(card_id),
                "result": "got_it",
            },
        )

        # Verify completed_suggested includes the card
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(1, overview.json()["completed_suggested"]["total_count"])

        # Soft-delete the card
        with TestingSessionLocal() as db:
            card = db.get(Card, card_id)
            card.status = "deleted"
            card.deleted_at = datetime.now(timezone.utc)
            db.commit()

        # After deletion, completed_suggested must be 0
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(0, overview.json()["completed_suggested"]["total_count"])

    def test_overview_suggested_reflects_only_active_cards_not_old_session(self):
        """suggested counts must come from active cards in session items, not stale session planned counts."""
        # 1 new + 2 strengthening gives select_review_cards 3 cards (new_quota=1 + 2 strengthening)
        card1 = self.create_card(content="new card", content_normalized="new card", review_state="new")
        card2 = self.create_card(content="strength one", content_normalized="strength one",
                                 review_state="strengthening", mastery_score=1, review_count=1)
        card3 = self.create_card(content="strength two", content_normalized="strength two",
                                 review_state="strengthening", mastery_score=1, review_count=1)

        # Create a daily_suggested session (today) — should have 3 items (1 new + 2 strengthening)
        daily = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5},
        ).json()
        self.assertEqual(3, daily["progress"]["total"])

        # Soft-delete card1 and card2
        with TestingSessionLocal() as db:
            for cid in (card1, card2):
                card = db.get(Card, cid)
                card.status = "deleted"
                card.deleted_at = datetime.now(timezone.utc)
            db.commit()

        # Overview must now show only 1 (card3), NOT 3 from old session
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        data = overview.json()
        self.assertEqual(1, data["suggested"]["total_count"],
                         "suggested.total_count should be 1 active card, not old session's 3")

    def test_overview_completed_unique_active_cards_not_cumulative(self):
        """Review 2 cards in one daily_suggested session → completed_suggested = 2, suggested = 2.
        Ghost card reviewed today + deleted must not inflate completed_suggested."""
        card1 = self.create_card(content="fresh new", content_normalized="fresh new", review_state="new")
        card2 = self.create_card(content="fresh strengthening", content_normalized="fresh strengthening",
                                 review_state="strengthening", mastery_score=1, review_count=1)

        # Review card1 and card2 via daily_suggested
        daily = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5},
        ).json()
        self.assertEqual(2, len(daily["items"]),
                         f"Expected 2 items (1 new + 1 strengthening), got {len(daily['items'])}")
        for item in daily["items"]:
            self.client.post(
                "/api/reviews/feedback",
                headers=self.auth_headers(),
                json={
                    "client_action_id": str(uuid4()),
                    "session_id": daily["session_id"],
                    "session_item_id": item["session_item_id"],
                    "card_id": str(item["card_id"]),
                    "result": "got_it",
                },
            )

        # Create a ghost card, review it today, then delete it — must not pollute completed_suggested
        ghost_id = self.create_card(content="ghost", content_normalized="ghost")
        ghost_session = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "restart": True},
        ).json()
        ghost_item = ghost_session["items"][0]
        self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": str(uuid4()),
                "session_id": ghost_session["session_id"],
                "session_item_id": ghost_item["session_item_id"],
                "card_id": str(ghost_item["card_id"]),
                "result": "got_it",
            },
        )
        with TestingSessionLocal() as db:
            ghost = db.get(Card, ghost_id)
            ghost.status = "deleted"
            ghost.deleted_at = datetime.now(timezone.utc)
            db.commit()

        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        data = overview.json()
        # 2 active cards reviewed (not 3 including ghost — ghost is deleted)
        self.assertEqual(2, data["completed_suggested"]["total_count"],
                         "completed_suggested total must exclude deleted ghost card")
        # Latest session (ghost) has 1 item for the deleted ghost → 0 active cards
        self.assertEqual(0, data["suggested"]["total_count"],
                         "suggested total from latest session with only deleted card = 0")

    def test_overview_suggested_correct_before_session_creation(self):
        """suggested.total_count must be correct even without creating a review session."""
        self.create_card(content="alpha", content_normalized="alpha")
        self.create_card(content="beta", content_normalized="beta")

        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        self.assertEqual(2, overview.json()["suggested"]["total_count"])


    # ===== Phase 6P-later-1: goal_progress tests =====

    def _today_noon_utc(self):
        local_zone = ZoneInfo("Asia/Shanghai")
        local_now = datetime.now(local_zone)
        local_noon = local_now.replace(
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        return local_noon.astimezone(timezone.utc)

    def test_goal_progress_default_daily_goal(self):
        self.create_card(content="gp default", content_normalized="gp default")
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertEqual(5, gp["target"])

    def test_goal_progress_daily_goal_3(self):
        overview = self.client.get(
            "/api/reviews/overview?daily_goal=3", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        self.assertEqual(3, overview.json()["goal_progress"]["target"])

    def test_goal_progress_daily_goal_10(self):
        overview = self.client.get(
            "/api/reviews/overview?daily_goal=10", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        self.assertEqual(10, overview.json()["goal_progress"]["target"])

    def test_goal_progress_invalid_daily_goal_fallback(self):
        for invalid in (7, 999):
            with self.subTest(daily_goal=invalid):
                overview = self.client.get(
                    f"/api/reviews/overview?daily_goal={invalid}",
                    headers=self.auth_headers(),
                )
                self.assertEqual(200, overview.status_code)
                self.assertEqual(5, overview.json()["goal_progress"]["target"])

    def test_goal_progress_zero_cards(self):
        overview = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertEqual(0, gp["completed_unique_today"])
        self.assertFalse(gp["has_goal_contributing_cards"])
        self.assertFalse(gp["has_any_reviewable_cards"])
        self.assertTrue(gp["is_goal_blocked"])

    def test_goal_progress_completed_counts_all_session_types(self):
        now = self._today_noon_utc()
        card_a = self.create_card(content="cs a", content_normalized="cs a")
        card_b = self.create_card(content="cs b", content_normalized="cs b")
        card_c = self.create_card(content="cs c", content_normalized="cs c")
        self.create_review_log(card_a, "got_it", now, session_type="daily_suggested")
        self.create_review_log(card_b, "fluent", now, session_type="new_only")
        self.create_review_log(card_c, "shaky", now, session_type="free_review")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertEqual(3, gp["completed_unique_today"])

    def test_goal_progress_same_card_not_double_counted(self):
        now = self._today_noon_utc()
        card = self.create_card(content="same card", content_normalized="same card")
        self.create_review_log(card, "got_it", now - timedelta(hours=2), session_type="daily_suggested")
        self.create_review_log(card, "fluent", now, session_type="free_review")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        self.assertEqual(1, overview.json()["goal_progress"]["completed_unique_today"])

    def test_goal_progress_deleted_card_not_counted(self):
        now = self._today_noon_utc()
        card_keep = self.create_card(content="keep", content_normalized="keep")
        card_del = self.create_card(content="delete me", content_normalized="delete me")
        self.create_review_log(card_keep, "got_it", now, session_type="daily_suggested")
        self.create_review_log(card_del, "fluent", now, session_type="daily_suggested")

        with TestingSessionLocal() as db:
            card = db.get(Card, card_del)
            card.deleted_at = now
            card.status = "deleted"
            db.commit()

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        self.assertEqual(1, overview.json()["goal_progress"]["completed_unique_today"])

    def test_goal_progress_is_goal_met(self):
        now = self._today_noon_utc()
        for i in range(5):
            card_id = self.create_card(content=f"met {i}", content_normalized=f"met {i}")
            self.create_review_log(card_id, "got_it", now, session_type="daily_suggested")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertTrue(gp["is_goal_met"])
        self.assertFalse(gp["is_overachieved"])
        self.assertEqual(5, gp["display_numerator"])

    def test_goal_progress_is_overachieved(self):
        now = self._today_noon_utc()
        for i in range(7):
            card_id = self.create_card(content=f"over {i}", content_normalized=f"over {i}")
            self.create_review_log(card_id, "got_it", now, session_type="daily_suggested")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertTrue(gp["is_overachieved"])
        self.assertEqual(5, gp["display_numerator"])
        self.assertEqual(5, gp["display_denominator"])
        self.assertEqual(7, gp["completed_unique_today"])

    def test_goal_progress_is_goal_blocked_true(self):
        now = self._today_noon_utc()
        for i in range(3):
            card_id = self.create_card(content=f"blocked {i}", content_normalized=f"blocked {i}")
            self.create_review_log(card_id, "got_it", now, session_type="daily_suggested")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertFalse(gp["is_goal_met"])
        self.assertTrue(gp["is_goal_blocked"])

    def test_goal_progress_is_goal_blocked_false_when_more_cards(self):
        now = self._today_noon_utc()
        card_a = self.create_card(content="reviewed a", content_normalized="reviewed a")
        card_b = self.create_card(content="unreviewed b", content_normalized="unreviewed b")
        self.create_review_log(card_a, "got_it", now, session_type="daily_suggested")
        # card_b not reviewed — still available

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertFalse(gp["is_goal_met"])
        self.assertFalse(gp["is_goal_blocked"])

    def test_goal_progress_all_reviewed_today(self):
        now = self._today_noon_utc()
        for i in range(3):
            card_id = self.create_card(content=f"all rev {i}", content_normalized=f"all rev {i}")
            self.create_review_log(card_id, "got_it", now, session_type="daily_suggested")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertFalse(gp["has_goal_contributing_cards"])
        self.assertTrue(gp["has_any_reviewable_cards"])

    def test_goal_progress_other_user_not_counted(self):
        now = self._today_noon_utc()
        my_card = self.create_card(content="my card", content_normalized="my card")
        other_card = self.create_card(
            content="other card", content_normalized="other card",
            user_id=self.other_user_uuid,
        )
        self.create_review_log(my_card, "got_it", now, session_type="daily_suggested")
        self.create_review_log(other_card, "fluent", now, user_id=self.other_user_uuid, session_type="daily_suggested")

        overview = self.client.get(
            "/api/reviews/overview?daily_goal=5", headers=self.auth_headers()
        )
        self.assertEqual(200, overview.status_code)
        gp = overview.json()["goal_progress"]
        self.assertEqual(1, gp["completed_unique_today"])

    def test_goal_progress_old_fields_unchanged(self):
        card_id = self.create_card(content="old fields", content_normalized="old fields")

        without_gp = self.client.get("/api/reviews/overview", headers=self.auth_headers())
        with_gp = self.client.get("/api/reviews/overview?daily_goal=3", headers=self.auth_headers())

        self.assertEqual(200, without_gp.status_code)
        self.assertEqual(200, with_gp.status_code)
        without = without_gp.json()
        with_gp_data = with_gp.json()

        for field in ("suggested", "completed_suggested", "extra_today", "is_all_done"):
            self.assertEqual(without[field], with_gp_data[field],
                             f"{field} must not change when daily_goal is passed")

        self.assertIsNone(with_gp_data["active_session"])
        self.assertIsNotNone(with_gp_data["goal_progress"])

    # ===== Phase 6P-later-2: daily_goal session tests =====

    def test_goal_session_no_daily_goal_old_behavior(self):
        """Not passing daily_goal keeps old limit-based behavior."""
        for i in range(5):
            self.create_card(
                content=f"old behavior {i}",
                content_normalized=f"old behavior {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(5, data["progress"]["total"])
        self.assertEqual(5, len(data["items"]))

    def test_goal_session_daily_goal_null_old_behavior(self):
        """Explicit daily_goal=null keeps old behavior."""
        for i in range(5):
            self.create_card(
                content=f"null goal {i}",
                content_normalized=f"null goal {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": None},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(5, resp.json()["progress"]["total"])

    def test_goal_session_daily_goal_abc_422(self):
        """Non-int daily_goal returns 422."""
        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": "abc"},
        )
        self.assertEqual(422, resp.status_code)

    def test_goal_session_daily_goal_7_fallback(self):
        """daily_goal=7 falls back to target=5."""
        for i in range(5):
            self.create_card(
                content=f"goal 7 fallback {i}",
                content_normalized=f"goal 7 fallback {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 7},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        # target=5 (fallback from 7), completed=0, remaining=5, effective_limit=5
        self.assertEqual(5, data["progress"]["total"])

    def test_goal_session_daily_goal_999_fallback(self):
        """daily_goal=999 falls back to target=5."""
        for i in range(5):
            self.create_card(
                content=f"goal 999 fallback {i}",
                content_normalized=f"goal 999 fallback {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 999},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(5, resp.json()["progress"]["total"])

    def test_goal_session_5_unreviewed_returns_5(self):
        """daily_goal=5, completed=0, 5 unreviewed → returns 5."""
        for i in range(5):
            self.create_card(
                content=f"goal 5 card {i}",
                content_normalized=f"goal 5 card {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(5, data["progress"]["total"])
        self.assertEqual(5, len(data["items"]))

    def test_goal_session_completed_4_fills_to_limit(self):
        """daily_goal=5, completed=4, 1 fresh card -> fills to limit when possible."""
        now = self._today_noon_utc()
        reviewed_ids = []
        for i in range(4):
            cid = self.create_card(
                content=f"reviewed {i}",
                content_normalized=f"reviewed {i}",
                review_state="reviewing" if i < 2 else "mastered",
                mastery_score=3 if i < 2 else 5,
                next_review_at=now + timedelta(days=7 + i),
            )
            self.create_review_log(cid, "got_it", now, session_type="daily_suggested")
            reviewed_ids.append(cid)
        new_id = self.create_card(
            content="unreviewed one",
            content_normalized="unreviewed one",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(5, data["progress"]["total"])
        item_ids = [UUID(item["card_id"]) for item in data["items"]]
        self.assertIn(new_id, item_ids)
        self.assertEqual(5, len(set(item_ids)))
        self.assertEqual(4, len([card_id for card_id in item_ids if card_id in reviewed_ids]))

    def test_goal_session_core_fill_reviewed_old_cards_after_goal_met(self):
        """limit=5, 5 reviewed old cards + 1 new card -> returns 5 with new card included."""
        now = self._today_noon_utc()
        old_ids = []
        for i in range(5):
            cid = self.create_card(
                content=f"old reviewed {i}",
                content_normalized=f"old reviewed {i}",
                review_state="reviewing" if i % 2 == 0 else "mastered",
                mastery_score=3 if i % 2 == 0 else 5,
                next_review_at=now + timedelta(days=10 + i),
            )
            self.create_review_log(cid, "got_it", now, session_type="daily_suggested")
            old_ids.append(cid)
        block_id = self.create_card(
            content="block",
            content_normalized="block",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        item_ids = [UUID(item["card_id"]) for item in data["items"]]
        self.assertEqual(5, data["progress"]["total"])
        self.assertIn(block_id, item_ids)
        self.assertEqual(5, len(set(item_ids)))
        self.assertEqual(4, len([card_id for card_id in item_ids if card_id in old_ids]))

    def test_goal_session_daily_goal_10_with_limit_5_returns_5(self):
        """Phase 8L: fill target follows limit, so daily_goal=10 no longer expands limit=5."""
        for i in range(10):
            self.create_card(
                content=f"goal 10 card {i}",
                content_normalized=f"goal 10 card {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 10},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(5, data["progress"]["total"])

    def test_goal_session_limit_10_returns_10(self):
        """The frontend dailyGoal=10 path sends limit=10 and still gets 10 cards."""
        for i in range(10):
            self.create_card(
                content=f"limit 10 card {i}",
                content_normalized=f"limit 10 card {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 10, "daily_goal": 10},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(10, resp.json()["progress"]["total"])

    def test_goal_session_limit_15_returns_15(self):
        """The frontend dailyGoal=15 path sends limit=15 and still gets 15 cards."""
        for i in range(15):
            self.create_card(
                content=f"limit 15 card {i}",
                content_normalized=f"limit 15 card {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 15, "daily_goal": 15},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(15, resp.json()["progress"]["total"])

    def test_goal_session_daily_goal_3_returns_3(self):
        """daily_goal=3, limit=5, completed=0, 5 unreviewed → returns 3."""
        for i in range(5):
            self.create_card(
                content=f"goal 3 card {i}",
                content_normalized=f"goal 3 card {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 3},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(3, data["progress"]["total"])

    def test_goal_session_resume_does_not_expand_existing_session(self):
        """restart=false returns the active session without adding cards for a larger current limit."""
        for i in range(5):
            self.create_card(
                content=f"resume original {i}",
                content_normalized=f"resume original {i}",
                review_state="new",
            )

        first = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, first.status_code, first.text)
        first_data = first.json()
        self.assertEqual(5, first_data["progress"]["total"])

        for i in range(10):
            self.create_card(
                content=f"resume extra {i}",
                content_normalized=f"resume extra {i}",
                review_state="new",
            )

        second = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 10, "daily_goal": 10},
        )
        self.assertEqual(200, second.status_code, second.text)
        second_data = second.json()
        self.assertEqual(first_data["session_id"], second_data["session_id"])
        self.assertEqual(5, second_data["progress"]["total"])
        self.assertEqual(5, len(second_data["items"]))

    def test_goal_session_completed_5_goal_mode_false(self):
        """daily_goal=5, completed=5 → goal_mode=False, old behavior, not blocked."""
        now = self._today_noon_utc()
        for i in range(5):
            cid = self.create_card(
                content=f"met {i}",
                content_normalized=f"met {i}",
            )
            self.create_review_log(cid, "got_it", now, session_type="daily_suggested")
        # Additional unreviewed cards for old logic to use
        self.create_card(
            content="extra new",
            content_normalized="extra new",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        # goal_mode=False, old behavior picks the extra new card
        self.assertIsNotNone(data["session_id"])
        self.assertGreater(data["progress"]["total"], 0)

    def test_goal_session_completed_7_goal_mode_false(self):
        """daily_goal=5, completed=7 → goal_mode=False, not blocked."""
        now = self._today_noon_utc()
        for i in range(7):
            cid = self.create_card(
                content=f"over {i}",
                content_normalized=f"over {i}",
            )
            self.create_review_log(cid, "got_it", now, session_type="daily_suggested")
        self.create_card(
            content="extra",
            content_normalized="extra",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertIsNotNone(resp.json()["session_id"],
                             "should not block session creation when overachieved")

    def test_goal_session_only_2_unreviewed_returns_2(self):
        """daily_goal=5, completed=0, only 2 unreviewed → returns 2, no error."""
        self.create_card(
            content="c1", content_normalized="c1", review_state="new"
        )
        self.create_card(
            content="c2", content_normalized="c2", review_state="new"
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(2, data["progress"]["total"])

    def test_goal_session_only_3_cards_returns_3(self):
        """limit=5, library has only 3 review-ready cards -> returns 3, no error."""
        for i in range(3):
            self.create_card(
                content=f"small library {i}",
                content_normalized=f"small library {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        self.assertEqual(3, resp.json()["progress"]["total"])

    def test_goal_session_zero_review_ready_cards(self):
        """daily_goal=5, completed=0, 0 review-ready → session_id=null, same as current."""
        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertIsNone(data["session_id"])
        self.assertEqual([], data["items"])

    def test_goal_session_strengthening_priority_over_reviewed(self):
        """Unreviewed strengthening comes before reviewed cards; reviewed cards may fill."""
        now = self._today_noon_utc()
        # reviewed strengthening card
        c_reviewed = self.create_card(
            content="reviewed strengthening",
            content_normalized="reviewed strengthening",
            review_state="strengthening",
            mastery_score=1,
            review_count=1,
        )
        self.create_review_log(c_reviewed, "shaky", now, session_type="daily_suggested")
        # unreviewed strengthening
        c_unreviewed = self.create_card(
            content="unreviewed strengthening",
            content_normalized="unreviewed strengthening",
            review_state="strengthening",
            mastery_score=1,
            review_count=1,
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        contents = {item["content"] for item in data["items"]}
        self.assertIn("unreviewed strengthening", contents)
        self.assertIn("reviewed strengthening", contents)
        ordered_contents = [item["content"] for item in data["items"]]
        self.assertLess(
            ordered_contents.index("unreviewed strengthening"),
            ordered_contents.index("reviewed strengthening"),
        )
        self.assertEqual(2, data["progress"]["total"])

    def test_goal_session_new_before_mastered_filler(self):
        """New cards (P3) should come before mastered filler (P4)."""
        # mastered filler: mastered but next_review_at far in future (not due)
        self.create_card(
            content="mastered filler",
            content_normalized="mastered filler",
            review_state="mastered",
            mastery_score=5,
            next_review_at=datetime(2126, 1, 1, tzinfo=timezone.utc),
        )
        # new card
        self.create_card(
            content="new card",
            content_normalized="new card",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(2, data["progress"]["total"])
        # new card (P3) should appear before mastered filler (P4)
        contents = [item["content"] for item in data["items"]]
        self.assertEqual(
            ["new card", "mastered filler"], contents,
            "P3 (new) must come before P4 (mastered filler)"
        )

    def test_goal_session_mastered_only_used_after_primary_candidates(self):
        """New, due, and strengthening cards all stay ahead of mastered fill cards."""
        self.create_card(
            content="mastered filler",
            content_normalized="mastered filler",
            review_state="mastered",
            mastery_score=5,
            next_review_at=NOW + timedelta(days=30),
        )
        self.create_card(
            content="due reviewing",
            content_normalized="due reviewing",
            review_state="reviewing",
            mastery_score=3,
            next_review_at=NOW - timedelta(days=1),
        )
        self.create_card(
            content="strengthening card",
            content_normalized="strengthening card",
            review_state="strengthening",
            mastery_score=1,
            last_review_result="shaky",
        )
        self.create_card(
            content="new card",
            content_normalized="new card",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        contents = [item["content"] for item in resp.json()["items"]]
        mastered_index = contents.index("mastered filler")
        for content in ("due reviewing", "strengthening card", "new card"):
            self.assertLess(contents.index(content), mastered_index)

    def test_goal_session_mastered_filler_non_due(self):
        """Mastered filler (P4) with next_review_at > now is selectable."""
        self.create_card(
            content="non-due mastered",
            content_normalized="non-due mastered",
            review_state="mastered",
            mastery_score=5,
            next_review_at=datetime(2126, 1, 1, tzinfo=timezone.utc),
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(1, data["progress"]["total"])
        self.assertEqual("non-due mastered", data["items"][0]["content"])

    def test_goal_session_fluent_filler_accurate_fields(self):
        """P5 fluent-like filler = reviewing + last_review_result=fluent, not due."""
        self.create_card(
            content="fluent card",
            content_normalized="fluent card",
            review_state="reviewing",
            mastery_score=4,
            last_review_result="fluent",
            next_review_at=datetime(2126, 1, 1, tzinfo=timezone.utc),
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(1, data["progress"]["total"])
        self.assertEqual("fluent card", data["items"][0]["content"])

    def test_goal_session_fills_with_reviewed_cards_when_needed(self):
        """Phase 8L product reversal: reviewed cards can fill the batch after fresh cards."""
        now = self._today_noon_utc()
        # 2 cards reviewed today
        for i in range(2):
            cid = self.create_card(
                content=f"reviewed {i}",
                content_normalized=f"reviewed {i}",
                review_state="reviewing",
                mastery_score=3,
                next_review_at=now + timedelta(days=7 + i),
            )
            self.create_review_log(cid, "got_it", now, session_type="daily_suggested")
        # 1 unreviewed card
        self.create_card(
            content="only unreviewed",
            content_normalized="only unreviewed",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(3, data["progress"]["total"])
        contents = [item["content"] for item in data["items"]]
        self.assertIn("only unreviewed", contents)
        self.assertIn("reviewed 0", contents)
        self.assertIn("reviewed 1", contents)

    def test_goal_session_dedup_p2_p4(self):
        """Mastered + due card appears in P2, not duplicated in P4."""
        self.create_card(
            content="due mastered",
            content_normalized="due mastered",
            review_state="mastered",
            mastery_score=5,
            next_review_at=NOW - timedelta(days=1),  # due
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(1, data["progress"]["total"],
                         "card in both P2 and P4 must only appear once")

    def test_goal_session_dedup_p4_p5(self):
        """Fluent-like reviewing card that is due appears in P2, not duplicated in P5."""
        self.create_card(
            content="fluent due",
            content_normalized="fluent due",
            review_state="reviewing",
            mastery_score=4,
            last_review_result="fluent",
            next_review_at=NOW - timedelta(days=1),  # due → P2
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(1, data["progress"]["total"],
                         "card in both P2 and P5 must only appear once")

    def test_goal_session_content_only_card(self):
        """Content-only card (no understanding) can enter goal_mode session."""
        self.create_card(
            content="content only",
            content_normalized="content only",
            understanding=None,
            is_review_ready=True,
            needs_manual_fix=False,
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(1, data["progress"]["total"])
        self.assertEqual("content only", data["items"][0]["content"])

    def test_goal_session_today_cross_session_type(self):
        """Cards reviewed via new_only/free_review are low-priority fill candidates."""
        now = self._today_noon_utc()
        # Card reviewed via new_only today
        cn = self.create_card(content="new only reviewed", content_normalized="new only reviewed")
        self.create_review_log(cn, "got_it", now, session_type="new_only")
        # Card reviewed via free_review today
        cf = self.create_card(content="free review reviewed", content_normalized="free review reviewed")
        self.create_review_log(cf, "fluent", now, session_type="free_review")
        # Unreviewed card
        self.create_card(
            content="fresh card",
            content_normalized="fresh card",
            review_state="new",
        )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "daily_goal": 5},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        contents = {item["content"] for item in data["items"]}
        self.assertIn("fresh card", contents)
        self.assertIn("new only reviewed", contents)
        self.assertIn("free review reviewed", contents)
        ordered_contents = [item["content"] for item in data["items"]]
        self.assertLess(ordered_contents.index("fresh card"), ordered_contents.index("new only reviewed"))
        self.assertLess(ordered_contents.index("fresh card"), ordered_contents.index("free review reviewed"))
        self.assertEqual(3, data["progress"]["total"])

    def test_goal_session_new_only_ignores_daily_goal(self):
        """new_only session type ignores daily_goal, still uses old limit."""
        for i in range(5):
            self.create_card(
                content=f"new only {i}",
                content_normalized=f"new only {i}",
                review_state="new",
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "new_only", "limit": 5, "daily_goal": 10},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        # new_only ignores daily_goal, still returns limit=5 cards
        self.assertEqual(5, data["progress"]["total"],
                         "new_only must ignore daily_goal and use limit=5")

    def test_goal_session_free_review_ignores_daily_goal(self):
        """free_review session type ignores daily_goal."""
        for i in range(5):
            self.create_card(
                content=f"free rv {i}",
                content_normalized=f"free rv {i}",
                review_state="reviewing",
                next_review_at=NOW - timedelta(days=1),
            )

        resp = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "free_review", "limit": 5, "daily_goal": 3},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()
        self.assertEqual(5, data["progress"]["total"],
                         "free_review must ignore daily_goal and use limit=5")

    def test_review_session_item_includes_where_encountered(self):
        self.create_card(
            content="look forward to",
            content_normalized="look forward to",
            where_encountered="NBA 解说",
            source_context="Fans look forward to the playoffs every year.",
            source_url="https://example.com/nba",
            example_sentence="I look forward to hearing from you.",
            example_translation="我期待收到你的回复。",
        )

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("NBA 解说", items[0]["where_encountered"])
        self.assertEqual(
            "Fans look forward to the playoffs every year.",
            items[0]["source_context"],
        )
        self.assertEqual("https://example.com/nba", items[0]["source_url"])
        self.assertEqual("I look forward to hearing from you.", items[0]["example_sentence"])
        self.assertEqual("我期待收到你的回复。", items[0]["example_translation"])

    def test_review_session_item_where_encountered_is_null_when_not_set(self):
        self.create_card(content="look up", content_normalized="look up")

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"session_type": "daily_suggested", "limit": 5, "restart": True},
        )

        self.assertEqual(200, response.status_code, response.text)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertIsNone(items[0]["where_encountered"])


class TodayReviewedApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="today-reviewed-secret-with-at-least-32-bytes",
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

        self._fixed_now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
        self._utc_patcher = patch('app.routers.reviews.utc_now', return_value=self._fixed_now)
        self._utc_patcher.start()

    def tearDown(self):
        self._utc_patcher.stop()
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def auth_headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def _now(self):
        return self._fixed_now

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
            "review_state": "reviewing",
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

    def test_requires_auth(self):
        response = self.client.get("/api/reviews/today-reviewed")
        self.assertEqual(401, response.status_code)

    def test_returns_today_reviewed_cards(self):
        now = self._now()
        card_a = self.create_card(content="hello world", understanding="你好世界")
        card_b = self.create_card(content="good morning", understanding="早上好")
        self.create_review_log(card_a, "got_it", now)
        self.create_review_log(card_b, "shaky", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(2, data["total"])
        contents = {item["content"] for item in data["items"]}
        self.assertIn("hello world", contents)
        self.assertIn("good morning", contents)

    def test_deduplicates_same_card(self):
        now = self._now()
        card = self.create_card(content="test card")
        self.create_review_log(card, "shaky", now - timedelta(hours=2))
        self.create_review_log(card, "got_it", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])

    def test_today_review_count_correct(self):
        now = self._now()
        card = self.create_card(content="multi review")
        self.create_review_log(card, "forgot", now - timedelta(hours=3))
        self.create_review_log(card, "shaky", now - timedelta(hours=2))
        self.create_review_log(card, "got_it", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual(3, data["items"][0]["today_review_count"])
        with TestingSessionLocal() as db:
            log_count = db.scalar(
                select(func.count()).select_from(ReviewLog).where(ReviewLog.card_id == card)
            )
            self.assertEqual(3, log_count)

    def test_returns_latest_result(self):
        now = self._now()
        card = self.create_card(content="result test")
        self.create_review_log(card, "forgot", now - timedelta(hours=2))
        self.create_review_log(card, "fluent", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        item = response.json()["items"][0]
        self.assertEqual("fluent", item["last_result"])
        self.assertEqual("很熟了", item["last_result_label"])

    def test_returns_current_card_content_not_snapshot(self):
        now = self._now()
        card_id = self.create_card(content="original content", understanding="original understanding")
        self.create_review_log(card_id, "got_it", now)

        with TestingSessionLocal() as db:
            db.execute(
                update(Card).where(Card.id == card_id).values(
                    content="edited content",
                    understanding="edited understanding",
                )
            )
            db.commit()

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        item = response.json()["items"][0]
        self.assertEqual("edited content", item["content"])
        self.assertEqual("edited understanding", item["understanding"])

    def test_excludes_deleted_cards(self):
        now = self._now()
        card_a = self.create_card(content="active card")
        card_b = self.create_card(content="deleted card")
        self.create_review_log(card_a, "got_it", now)
        self.create_review_log(card_b, "shaky", now)

        with TestingSessionLocal() as db:
            db.execute(
                update(Card).where(Card.id == card_b).values(
                    deleted_at=now, status="deleted"
                )
            )
            db.commit()

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual("active card", data["items"][0]["content"])

    def test_excludes_non_today_reviews(self):
        now = self._now()
        card_today = self.create_card(content="today card")
        card_yesterday = self.create_card(content="yesterday card")
        self.create_review_log(card_today, "got_it", now)
        self.create_review_log(card_yesterday, "got_it", now - timedelta(days=2))

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual("today card", data["items"][0]["content"])

    def test_excludes_other_user_cards(self):
        now = self._now()
        card = self.create_card(content="my card")
        self.create_review_log(card, "got_it", now)

        other_card = self.create_card(
            content="other card", user_id=self.other_user_uuid
        )
        self.create_review_log(other_card, "got_it", now, user_id=self.other_user_uuid)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(1, data["total"])
        self.assertEqual("my card", data["items"][0]["content"])

    def test_returns_where_encountered(self):
        now = self._now()
        card = self.create_card(
            content="clutch",
            understanding="关键时刻顶得住",
            where_encountered="NBA解说",
            source_context="He made a clutch shot with two seconds left.",
            source_url="https://example.com/highlights?t=118",
            example_sentence="She delivered a clutch performance in the final.",
            example_translation="她在决赛中上演了关键表现。",
        )
        self.create_review_log(card, "got_it", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        item = response.json()["items"][0]
        self.assertEqual("NBA解说", item["where_encountered"])
        self.assertEqual(
            "He made a clutch shot with two seconds left.",
            item["source_context"],
        )
        self.assertEqual("https://example.com/highlights?t=118", item["source_url"])
        self.assertEqual(
            "She delivered a clutch performance in the final.",
            item["example_sentence"],
        )
        self.assertEqual("她在决赛中上演了关键表现。", item["example_translation"])

    def test_where_encountered_null_when_not_set(self):
        now = self._now()
        card = self.create_card(content="no source", understanding="no source")
        self.create_review_log(card, "got_it", now)

        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        item = response.json()["items"][0]
        self.assertIsNone(item["where_encountered"])

    def test_empty_today(self):
        response = self.client.get("/api/reviews/today-reviewed", headers=self.auth_headers())
        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(0, data["total"])
        self.assertEqual([], data["items"])


if __name__ == "__main__":
    unittest.main()
