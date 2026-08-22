"""Minimal PostgreSQL integration safety net for core persisted data.

These tests intentionally do not override ``get_db``. They must only run
against the isolated database created by scripts/run-postgresql-tests.ps1.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings as app_settings
from app.database import DATABASE_DIALECT, SessionLocal, engine
from app.main import app
from app.models.card import Card
from app.models.resource_usage import ResourceUsage
from app.models.review import ClientAction, ReviewLog, ReviewSession, ReviewSessionItem
from app.models.user import User
from app.services import auth_service
from app.services.security import consume_daily_quota, rate_limiter


EXPECTED_TEST_DATABASE = "english_analyzer_phase1_pytest"


@pytest.fixture(autouse=True)
def postgresql_only(monkeypatch):
    if DATABASE_DIALECT != "postgresql":
        pytest.skip("PostgreSQL integration tests require PostgreSQL")
    with engine.connect() as connection:
        database_name = connection.execute(text("SELECT current_database()")).scalar_one()
    if database_name != EXPECTED_TEST_DATABASE:
        pytest.fail(
            "Refusing to run PostgreSQL integration test against "
            f"database '{database_name}', expected '{EXPECTED_TEST_DATABASE}'"
        )
    rate_limiter.reset()
    monkeypatch.setattr(
        auth_service,
        "settings",
        replace(
            app_settings,
            jwt_secret_key="postgres-integration-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        ),
    )


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _create_user(prefix: str = "pg-user") -> tuple[UUID, str]:
    user_id = uuid4()
    with SessionLocal() as db:
        db.add(User(id=user_id, wx_openid=f"{prefix}-{user_id}"))
        db.commit()
    return user_id, auth_service.create_access_token(user_id)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_review_ready_card(user_id: UUID, content: str = "review target") -> UUID:
    with SessionLocal() as db:
        card = Card(
            user_id=user_id,
            content=content,
            content_normalized=content.lower(),
            card_type="word",
            understanding="meaning",
            translation="translation",
            analysis_status="done",
            analysis_level="pass",
            analysis_messages=["postgres-jsonb-ok"],
            understanding_source="ai",
            is_review_ready=True,
            needs_manual_fix=False,
            review_state="new",
            next_review_at=datetime.now(timezone.utc),
            status="active",
        )
        db.add(card)
        db.commit()
        return card.id


def test_postgresql_cards_crud_happy_path(client: TestClient):
    user_id, token = _create_user("pg-cards")
    payload = {
        "content": "Crave",
        "card_type": "word",
        "translation": "渴望",
        "understanding": "want strongly",
        "analysis_status": "done",
        "analysis_level": "pass",
        "analysis_messages": ["created by postgresql integration"],
        "understanding_source": "ai",
        "where_encountered": "PostgreSQL integration",
    }

    created = client.post("/api/cards", headers=_auth_headers(token), json=payload)
    assert created.status_code == 200, created.text
    data = created.json()
    card_id = data["id"]
    assert data["user_id"] == str(user_id)
    assert data["analysis_messages"] == ["created by postgresql integration"]
    assert data["version"] == 1
    assert data["created_at"].endswith("Z") or "+" in data["created_at"]

    fetched = client.get(f"/api/cards/{card_id}", headers=_auth_headers(token))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["content"] == "Crave"

    updated = client.patch(
        f"/api/cards/{card_id}",
        headers=_auth_headers(token),
        json={"base_version": data["version"], "content": "Craving", "note": "updated"},
    )
    assert updated.status_code == 200, updated.text
    updated_data = updated.json()
    assert updated_data["content"] == "Craving"
    assert updated_data["content_normalized"] == "craving"
    assert updated_data["analysis_status"] == "pending"
    assert updated_data["version"] == 2

    deleted = client.delete(
        f"/api/cards/{card_id}",
        headers=_auth_headers(token),
        params={"base_version": updated_data["version"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert deleted.json()["deleted_at"] is not None

    with SessionLocal() as db:
        card = db.get(Card, UUID(card_id))
        assert card is not None
        assert card.deleted_at is not None
        assert card.status == "deleted"
        assert card.analysis_messages == ["created by postgresql integration"]


def test_postgresql_card_sync_replays_client_action_json_payload(client: TestClient):
    user_id, token = _create_user("pg-card-sync")
    action_id = f"pg-sync-{uuid4()}"
    payload = {
        "client_action_id": action_id,
        "operation": "CREATE",
        "local_id": f"local-{uuid4()}",
        "payload": {
            "content": "sync phrase",
            "card_type": "phrase",
            "translation": "同步短语",
            "analysis_status": "done",
            "analysis_level": "pass",
            "analysis_messages": ["sync-jsonb"],
            "understanding_source": "ai",
        },
    }

    first = client.post("/api/cards/sync", headers=_auth_headers(token), json=payload)
    second = client.post("/api/cards/sync", headers=_auth_headers(token), json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_data = first.json()
    second_data = second.json()
    assert second_data["replayed"] is True
    assert second_data["card"]["id"] == first_data["card"]["id"]

    with SessionLocal() as db:
        actions = list(
            db.scalars(
                select(ClientAction).where(
                    ClientAction.user_id == user_id,
                    ClientAction.client_action_id == action_id,
                )
            )
        )
        assert len(actions) == 1
        action = actions[0]
        assert action.status == "succeeded"
        assert action.request_payload["operation"] == "CREATE"
        assert action.response_payload["card"]["id"] == first_data["card"]["id"]
        assert (
            db.scalar(select(func.count()).select_from(Card).where(Card.user_id == user_id))
            == 1
        )


def test_postgresql_review_feedback_writes_multitable_transaction(client: TestClient):
    user_id, token = _create_user("pg-review")
    card_id = _create_review_ready_card(user_id, "feedback card")

    session_response = client.post(
        "/api/review-sessions",
        headers=_auth_headers(token),
        json={"session_type": "daily_suggested", "limit": 5, "restart": True},
    )
    assert session_response.status_code == 200, session_response.text
    session_data = session_response.json()
    item = session_data["items"][0]
    assert item["card_id"] == str(card_id)

    action_id = f"pg-review-{uuid4()}"
    feedback = client.post(
        "/api/reviews/feedback",
        headers=_auth_headers(token),
        json={
            "client_action_id": action_id,
            "session_id": session_data["session_id"],
            "session_item_id": item["session_item_id"],
            "card_id": item["card_id"],
            "result": "got_it",
        },
    )
    assert feedback.status_code == 200, feedback.text

    with SessionLocal() as db:
        card = db.get(Card, card_id)
        session = db.get(ReviewSession, UUID(session_data["session_id"]))
        session_item = db.get(ReviewSessionItem, UUID(item["session_item_id"]))
        log = db.scalar(select(ReviewLog).where(ReviewLog.card_id == card_id))
        action = db.scalar(
            select(ClientAction).where(
                ClientAction.user_id == user_id,
                ClientAction.client_action_id == action_id,
            )
        )
        assert card is not None
        assert card.review_count == 1
        assert card.last_review_result == "got_it"
        assert session is not None
        assert session.reviewed_count == 1
        assert session_item is not None
        assert session_item.status == "done"
        assert session_item.result == "got_it"
        assert log is not None
        assert log.result == "got_it"
        assert log.card_snapshot["content"] == "feedback card"
        assert action is not None
        assert action.status == "succeeded"
        assert action.response_payload["status"] == "success"


def test_postgresql_history_queries_latest_log_date_range_and_user_isolation(client: TestClient):
    user_id, token = _create_user("pg-history")
    other_user_id, other_token = _create_user("pg-history-other")
    card_id = _create_review_ready_card(user_id, "history card")
    other_card_id = _create_review_ready_card(other_user_id, "other history card")

    def review_once(user_token: str, result: str = "got_it") -> str:
        session_response = client.post(
            "/api/review-sessions",
            headers=_auth_headers(user_token),
            json={"session_type": "daily_suggested", "limit": 5, "restart": True},
        )
        assert session_response.status_code == 200, session_response.text
        session_data = session_response.json()
        item = session_data["items"][0]
        feedback = client.post(
            "/api/reviews/feedback",
            headers=_auth_headers(user_token),
            json={
                "client_action_id": f"pg-history-{uuid4()}",
                "session_id": session_data["session_id"],
                "session_item_id": item["session_item_id"],
                "card_id": item["card_id"],
                "result": result,
            },
        )
        assert feedback.status_code == 200, feedback.text
        with SessionLocal() as db:
            return str(
                db.scalar(
                    select(ReviewLog.id)
                    .where(ReviewLog.card_id == UUID(item["card_id"]))
                    .order_by(ReviewLog.reviewed_at.desc())
                    .limit(1)
                )
            )

    log_id = review_once(token, "got_it")
    other_log_id = review_once(other_token, "forgot")
    today = date.today().isoformat()

    history = client.get(
        "/api/reviews/history",
        headers=_auth_headers(token),
        params={"date_from": today, "date_to": today},
    )
    assert history.status_code == 200, history.text
    history_data = history.json()
    assert history_data["total"] == 1
    assert history_data["items"][0]["card_id"] == str(card_id)
    assert history_data["items"][0]["review_log_id"] == log_id

    summary = client.get(
        "/api/reviews/history/summary",
        headers=_auth_headers(token),
        params={"date_from": today, "date_to": today},
    )
    assert summary.status_code == 200, summary.text
    summary_data = summary.json()
    assert summary_data["total_reviews"] == 1
    assert summary_data["unique_cards"] == 1
    assert summary_data["latest_result_card_counts"]["got_it"] == 1

    detail = client.get(f"/api/reviews/history/{log_id}", headers=_auth_headers(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["card"]["content"] == "history card"

    isolated = client.get(f"/api/reviews/history/{other_log_id}", headers=_auth_headers(token))
    assert isolated.status_code == 404
    other_detail = client.get(f"/api/reviews/history/{other_log_id}", headers=_auth_headers(other_token))
    assert other_detail.status_code == 200, other_detail.text
    assert other_detail.json()["card"]["card_id"] == str(other_card_id)


def test_postgresql_review_feedback_rolls_back_on_multitable_failure(client: TestClient):
    user_id, token = _create_user("pg-rollback")
    card_id = _create_review_ready_card(user_id, "rollback card")

    session_response = client.post(
        "/api/review-sessions",
        headers=_auth_headers(token),
        json={"session_type": "daily_suggested", "limit": 5, "restart": True},
    )
    assert session_response.status_code == 200, session_response.text
    session_data = session_response.json()
    item = session_data["items"][0]
    assert item["card_id"] == str(card_id)
    action_id = f"pg-rollback-{uuid4()}"

    with SessionLocal() as db:
        session = db.get(ReviewSession, UUID(session_data["session_id"]))
        session_item = db.get(ReviewSessionItem, UUID(item["session_item_id"]))
        card = db.get(Card, card_id)
        assert session is not None
        assert session_item is not None
        assert card is not None

        db.add(
            ClientAction(
                user_id=user_id,
                client_action_id=action_id,
                action_type="review_feedback",
                request_payload={"source": "postgres-rollback-test"},
                status="processing",
            )
        )
        card.review_count = 99
        session.reviewed_count = 99
        session_item.status = "done"
        session_item.result = "forgot"
        db.add(
            ReviewLog(
                user_id=user_id,
                card_id=card.id,
                session_id=session.id,
                session_item_id=session_item.id,
                session_type=session.session_type,
                result="forgot",
                reviewed_at=datetime.now(timezone.utc),
                card_snapshot={"content": card.content},
                card_state_before_review="new",
                review_state_before="new",
                review_state_after="strengthening",
                mastery_score_before=0,
                mastery_score_after=0,
                recovery_stage_before=0,
                recovery_stage_after=1,
            )
        )
        db.add(
            ReviewSessionItem(
                session_id=session.id,
                card_id=card.id,
                position=session_item.position,
                status="pending",
            )
        )

        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    with SessionLocal() as db:
        item_after = db.get(ReviewSessionItem, UUID(item["session_item_id"]))
        card_after = db.get(Card, card_id)
        session_after = db.get(ReviewSession, UUID(session_data["session_id"]))
        assert item_after is not None
        assert item_after.status == "pending"
        assert item_after.result is None
        assert card_after is not None
        assert card_after.review_count == 0
        assert session_after is not None
        assert session_after.reviewed_count == 0
        assert (
            db.scalar(select(func.count()).select_from(ReviewLog).where(ReviewLog.user_id == user_id))
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ClientAction)
                .where(
                    ClientAction.user_id == user_id,
                    ClientAction.client_action_id == action_id,
                )
            )
            == 0
        )


def test_postgresql_resource_usage_quota_unique_increment_and_limit():
    user_id, _ = _create_user("pg-quota")

    with SessionLocal() as db:
        consume_daily_quota(db, user_id=user_id, resource="ai", limit=2)
        consume_daily_quota(db, user_id=user_id, resource="ai", limit=2)
        usage = db.scalar(
            select(ResourceUsage).where(
                ResourceUsage.user_id == user_id,
                ResourceUsage.resource == "ai",
                ResourceUsage.usage_date == date.today(),
            )
        )
        assert usage is not None
        assert usage.count == 2
        assert usage.updated_at.tzinfo is not None

        with pytest.raises(Exception):
            consume_daily_quota(db, user_id=user_id, resource="ai", limit=2)

    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ResourceUsage)
                .where(
                    ResourceUsage.user_id == user_id,
                    ResourceUsage.resource == "ai",
                    ResourceUsage.usage_date == date.today(),
                )
            )
            == 1
        )
        duplicate = ResourceUsage(
            user_id=user_id,
            resource="ai",
            usage_date=date.today(),
            count=0,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
