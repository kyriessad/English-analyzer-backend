from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import settings as app_settings
from app.database import SessionLocal
from app.main import app
from app.models.card import Card
from app.models.review import CardFsrsState, ReviewAnswerLog, ReviewMcqQuestion, ReviewSessionItem
from app.models.user import User, utc_now
from app.services import auth_service
pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for migrated PostgreSQL Review V1 tests",
)


def _cleanup_user(user_id: UUID) -> None:
    with SessionLocal() as db:
        db.execute(text("DELETE FROM review_answer_logs WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM review_mcq_questions WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM card_fsrs_states WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM review_logs WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM review_records WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM client_actions WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(
            text(
                """
                DELETE FROM review_session_items
                USING review_sessions
                WHERE review_session_items.session_id = review_sessions.id
                  AND review_sessions.user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        db.execute(text("DELETE FROM review_sessions WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(
            text(
                """
                DELETE FROM card_lexical_metadata
                USING cards
                WHERE card_lexical_metadata.card_id = cards.id
                  AND cards.user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        db.execute(text("DELETE FROM cards WHERE user_id = :user_id"), {"user_id": user_id})
        db.execute(text("DELETE FROM users WHERE id = :user_id"), {"user_id": user_id})
        db.commit()


def _create_user() -> tuple[UUID, dict[str, str]]:
    user_id = uuid4()
    with SessionLocal() as db:
        db.add(User(id=user_id, wx_openid=f"pg-review-v1-{user_id}", timezone="UTC"))
        db.commit()
    token = auth_service.create_access_token(user_id)
    return user_id, {"Authorization": f"Bearer {token}"}


def _create_card(
    user_id: UUID,
    content: str,
    understanding: str | None,
    card_type: str = "word",
    created_at: datetime | None = None,
) -> UUID:
    with SessionLocal() as db:
        card = Card(
            user_id=user_id,
            content=content,
            content_normalized=" ".join(content.strip().lower().split()),
            card_type=card_type,
            understanding=understanding,
            analysis_status="done",
            is_review_ready=True,
            needs_manual_fix=False,
            analysis_level="pass",
            analysis_messages=[],
            understanding_source="user",
            review_state="new",
            mastery_score=0,
            recovery_stage=0,
            review_count=0,
            forgot_count=0,
            shaky_count=0,
            got_it_count=0,
            fluent_count=0,
            again_count=0,
            hard_count=0,
            good_count=0,
            easy_count=0,
            next_review_at=utc_now(),
            created_at=created_at or utc_now(),
            status="active",
        )
        db.add(card)
        db.commit()
        db.refresh(card)
        return card.id


@pytest.fixture(autouse=True)
def _auth_settings():
    original = auth_service.settings
    auth_service.settings = replace(
        app_settings,
        jwt_secret_key="review-v1-postgresql-secret-with-at-least-32-bytes",
        jwt_algorithm="HS256",
        jwt_expire_days=3,
    )
    try:
        yield
    finally:
        auth_service.settings = original


def test_postgresql_review_v1_mcq_ecdict_repeat_snapshot_and_delete():
    user_id, headers = _create_user()
    client = TestClient(app)
    try:
        target_id = _create_card(
            user_id,
            "abandon",
            "完整的用户理解",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        _create_card(user_id, "borrow", "借入")
        _create_card(user_id, "empty-understanding", "")

        response = client.post("/api/review-sessions", headers=headers, json={"limit": 1, "restart": True})
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["card_id"] == str(target_id)
        assert len(item["options"]) == 4
        assert "完整的用户理解" in [option["text"] for option in item["options"]]

        with SessionLocal() as db:
            question = db.scalar(select(ReviewMcqQuestion).where(ReviewMcqQuestion.card_id == target_id))
            assert question is not None
            sources = {option["source"] for option in question.options_snapshot}
            assert "understanding" in sources
            assert "user_card" in sources
            assert "ecdict" in sources

        wrong = next(option for option in item["options"] if option["option_id"] != "correct")
        first_wrong = client.post(
            "/api/reviews/feedback",
            headers=headers,
            json={
                "client_action_id": f"pg-answer-{uuid4()}",
                "session_id": response.json()["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "question_id": item["question_id"],
                "selected_option_id": wrong["option_id"],
                "response_time_ms": 111,
            },
        )
        assert first_wrong.status_code == 200, first_wrong.text
        repeat = first_wrong.json()["next_item"]
        while repeat is not None and not repeat["is_repeat"]:
            correct = next(option for option in repeat["options"] if option["option_id"] == "correct")
            answered = client.post(
                "/api/reviews/feedback",
                headers=headers,
                json={
                    "client_action_id": f"pg-answer-{uuid4()}",
                    "session_id": response.json()["session_id"],
                    "session_item_id": repeat["session_item_id"],
                    "card_id": repeat["card_id"],
                    "question_id": repeat["question_id"],
                    "selected_option_id": correct["option_id"],
                },
            )
            assert answered.status_code == 200, answered.text
            repeat = answered.json()["next_item"]
        assert repeat is not None
        assert repeat["is_repeat"] is True
        assert repeat["attempt_no"] == 2

        repeat_wrong = next(option for option in repeat["options"] if option["option_id"] != "correct")
        second_wrong = client.post(
            "/api/reviews/feedback",
            headers=headers,
            json={
                "client_action_id": f"pg-answer-{uuid4()}",
                "session_id": response.json()["session_id"],
                "session_item_id": repeat["session_item_id"],
                "card_id": repeat["card_id"],
                "question_id": repeat["question_id"],
                "selected_option_id": repeat_wrong["option_id"],
            },
        )
        assert second_wrong.status_code == 200, second_wrong.text
        assert second_wrong.json()["done"] is True

        with SessionLocal() as db:
            logs = list(db.scalars(select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == target_id)))
            assert len(logs) == 2
            assert [log.fsrs_rating for log in logs] == ["Again", "Again"]
            assert logs[0].prompt_content_snapshot == "abandon"
            assert logs[0].correct_answer_snapshot == "完整的用户理解"
            assert len(logs[0].options_snapshot) == 4
            assert len(logs[0].option_order) == 4
            assert logs[0].selected_answer_text
            assert logs[0].fsrs_state_before_json
            assert logs[0].fsrs_state_after_json
            state = db.scalar(select(CardFsrsState).where(CardFsrsState.card_id == target_id))
            assert state is not None
            repeat_items = list(
                db.scalars(
                    select(ReviewSessionItem).where(
                        ReviewSessionItem.card_id == target_id,
                        ReviewSessionItem.is_repeat.is_(True),
                    )
                )
            )
            assert len(repeat_items) == 1
            assert repeat_items[0].repeat_count == 1
            assert repeat_items[0].reappear_count == 1

        deleted = client.delete(f"/api/cards/{target_id}", headers=headers)
        assert deleted.status_code == 200, deleted.text

        with SessionLocal() as db:
            logs_after_delete = list(
                db.scalars(select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == target_id))
            )
            assert len(logs_after_delete) == 2
            assert logs_after_delete[0].prompt_content_snapshot == "abandon"
            assert logs_after_delete[0].correct_answer_snapshot == "完整的用户理解"
            assert len(logs_after_delete[0].options_snapshot) == 4
            assert logs_after_delete[0].selected_answer_text
            assert logs_after_delete[0].is_correct is False
    finally:
        _cleanup_user(user_id)


def test_postgresql_level7_audit_accepts_v1_answer_log_and_detects_partial_state():
    user_id, headers = _create_user()
    client = TestClient(app)
    try:
        target_id = _create_card(
            user_id,
            "audit-v1",
            "审计快照",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        _create_card(user_id, "audit-borrow", "借入")
        _create_card(user_id, "audit-capture", "捕获")
        _create_card(user_id, "audit-deliver", "递送")

        session_response = client.post(
            "/api/review-sessions", headers=headers, json={"limit": 1, "restart": True}
        )
        assert session_response.status_code == 200, session_response.text
        session = session_response.json()
        item = session["items"][0]
        correct = next(option for option in item["options"] if option["option_id"] == "correct")
        feedback = client.post(
            "/api/reviews/feedback",
            headers=headers,
            json={
                "client_action_id": f"pg-audit-{uuid4()}",
                "session_id": session["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "question_id": item["question_id"],
                "selected_option_id": correct["option_id"],
            },
        )
        assert feedback.status_code == 200, feedback.text

        with SessionLocal() as db:
            reviewed_item_without_log = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM review_session_items i
                    WHERE i.session_id = :session_id
                      AND i.status IN ('reviewed', 'done')
                      AND NOT EXISTS (
                        SELECT 1 FROM review_logs l
                        WHERE l.card_id = i.card_id
                          AND l.session_id = i.session_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM review_answer_logs a
                        WHERE a.session_item_id = i.id
                      )
                    """
                ),
                {"session_id": session["session_id"]},
            ).scalar_one()
            assert reviewed_item_without_log == 0

            answer_log_without_reviewed_item = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM review_answer_logs a
                    WHERE a.session_id = :session_id
                      AND NOT EXISTS (
                        SELECT 1 FROM review_session_items i
                        WHERE i.id = a.session_item_id
                          AND i.status IN ('reviewed', 'done')
                      )
                    """
                ),
                {"session_id": session["session_id"]},
            ).scalar_one()
            assert answer_log_without_reviewed_item == 0

            answer_log = db.scalar(
                select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == target_id)
            )
            assert answer_log is not None
            db.delete(answer_log)
            db.flush()
            reviewed_item_without_log_after_delete = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM review_session_items i
                    WHERE i.session_id = :session_id
                      AND i.status IN ('reviewed', 'done')
                      AND NOT EXISTS (
                        SELECT 1 FROM review_logs l
                        WHERE l.card_id = i.card_id
                          AND l.session_id = i.session_id
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM review_answer_logs a
                        WHERE a.session_item_id = i.id
                      )
                    """
                ),
                {"session_id": session["session_id"]},
            ).scalar_one()
            assert reviewed_item_without_log_after_delete == 1
            db.rollback()
    finally:
        _cleanup_user(user_id)


def test_postgresql_review_v1_latest_card_content_understanding_and_stale_question():
    user_id, headers = _create_user()
    client = TestClient(app)
    try:
        ids = [
            _create_card(user_id, "original content", "原始理解"),
            _create_card(user_id, "borrow", "借入"),
            _create_card(user_id, "capture", "捕获"),
            _create_card(user_id, "deliver", "递送"),
        ]
        response = client.post("/api/review-sessions", headers=headers, json={"limit": 4, "restart": True})
        assert response.status_code == 200, response.text
        first = response.json()["items"][0]

        with SessionLocal() as db:
            card = db.get(Card, ids[0])
            card.content = "updated content"
            card.content_normalized = "updated content"
            card.understanding = "最新理解"
            db.commit()

        stale = client.post(
            "/api/reviews/feedback",
            headers=headers,
            json={
                "client_action_id": f"pg-stale-{uuid4()}",
                "session_id": response.json()["session_id"],
                "session_item_id": first["session_item_id"],
                "card_id": first["card_id"],
                "question_id": first["question_id"],
                "selected_option_id": first["options"][0]["option_id"],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "stale_question"

        refreshed = client.get("/api/reviews/today", headers=headers, params={"limit": 4})
        assert refreshed.status_code == 200, refreshed.text
        refreshed_first = refreshed.json()["items"][0]
        assert refreshed_first["content"] == "updated content"
        assert refreshed_first["understanding"] == "最新理解"
        assert "最新理解" in [option["text"] for option in refreshed_first["options"]]
    finally:
        _cleanup_user(user_id)


def test_postgresql_review_v1_understanding_cleared_before_display_is_excluded():
    user_id, headers = _create_user()
    client = TestClient(app)
    try:
        ids = [
            _create_card(user_id, "abandon", "放弃"),
            _create_card(user_id, "borrow", "借入"),
            _create_card(user_id, "capture", "捕获"),
            _create_card(user_id, "deliver", "递送"),
        ]
        response = client.post("/api/review-sessions", headers=headers, json={"limit": 4, "restart": True})
        assert response.status_code == 200, response.text

        with SessionLocal() as db:
            card = db.get(Card, ids[0])
            card.understanding = ""
            db.commit()

        refreshed = client.get("/api/reviews/today", headers=headers, params={"limit": 4})
        assert refreshed.status_code == 200, refreshed.text
        returned = {UUID(item["card_id"]) for item in refreshed.json()["items"]}
        assert ids[0] not in returned
        assert all(item["understanding"] for item in refreshed.json()["items"])
    finally:
        _cleanup_user(user_id)
