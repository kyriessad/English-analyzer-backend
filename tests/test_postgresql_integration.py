"""Minimal PostgreSQL integration safety net for core persisted data.

These tests intentionally do not override ``get_db``. They must only run
against the isolated database created by scripts/run-postgresql-tests.ps1.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
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
from app.services.security import check_daily_quota, consume_daily_quota, rate_limiter


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


def _quota_count(user_id: UUID) -> int:
    with SessionLocal() as db:
        usage = db.scalar(
            select(ResourceUsage).where(
                ResourceUsage.user_id == user_id,
                ResourceUsage.resource == "ai",
                ResourceUsage.usage_date == date.today(),
            )
        )
        return 0 if usage is None else usage.count


def _analyze_payload(suffix: str = "") -> dict:
    return {
        "text": f"quota boundary experiment {suffix}".strip(),
        "cardType": "auto",
        "targetLang": "zh",
    }


def test_postgresql_quota_precheck_does_not_increment():
    user_id, _ = _create_user("pg-quota-precheck")
    before_count = _quota_count(user_id)
    assert before_count == 0
    with SessionLocal() as db:
        check_daily_quota(db, user_id=user_id, resource="ai", limit=2)
    after_count = _quota_count(user_id)
    assert after_count == 0


def test_postgresql_ai_slot_failure_does_not_increment(client, monkeypatch):
    user_id, token = _create_user("pg-quota-slot-failure")

    @asynccontextmanager
    async def rejected_slot(*args, **kwargs):
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="slot full")
        yield

    monkeypatch.setattr("app.main.async_resource_slot", rejected_slot)
    before_count = _quota_count(user_id)
    response = client.post(
        "/api/analyze-english",
        headers=_auth_headers(token),
        json=_analyze_payload(),
    )
    assert response.status_code == 503, response.text
    after_count = _quota_count(user_id)
    assert before_count == 0
    assert after_count == 0


def test_postgresql_non_stream_ai_failure_keeps_committed_quota(monkeypatch):
    user_id, token = _create_user("pg-quota-non-stream-failure")
    events: list[str] = []

    original_check = check_daily_quota
    original_consume = consume_daily_quota

    def tracked_check(*args, **kwargs):
        events.append("quota_precheck")
        return original_check(*args, **kwargs)

    @asynccontextmanager
    async def tracked_slot(*args, **kwargs):
        events.append("slot_acquired")
        try:
            yield
        finally:
            events.append("slot_released")

    def tracked_consume(*args, **kwargs):
        events.append("quota_consume")
        return original_consume(*args, **kwargs)

    def failing_analyze(**kwargs):
        events.append("analyze_start")
        raise RuntimeError("deterministic non-stream AI failure")

    monkeypatch.setattr("app.main.check_daily_quota", tracked_check)
    monkeypatch.setattr("app.main.async_resource_slot", tracked_slot)
    monkeypatch.setattr("app.main.consume_daily_quota", tracked_consume)
    monkeypatch.setattr("app.main.analyze_text", failing_analyze)
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/analyze-english",
            headers=_auth_headers(token),
            json=_analyze_payload("non-stream-failure"),
        )
    assert response.status_code == 500
    assert _quota_count(user_id) == 1
    assert events == ["quota_precheck", "slot_acquired", "quota_consume", "analyze_start", "slot_released"]


def test_postgresql_stream_ai_failure_keeps_committed_quota(monkeypatch):
    user_id, token = _create_user("pg-quota-stream-failure")
    events: list[str] = []

    original_check = check_daily_quota
    original_consume = consume_daily_quota

    def tracked_check(*args, **kwargs):
        events.append("quota_precheck")
        return original_check(*args, **kwargs)

    @asynccontextmanager
    async def tracked_slot(*args, **kwargs):
        events.append("slot_acquired")
        try:
            yield
        finally:
            events.append("slot_released")

    def tracked_consume(*args, **kwargs):
        events.append("quota_consume")
        return original_consume(*args, **kwargs)

    def failing_stream(**kwargs):
        events.append("analyze_start")
        yield ("field", "translation", "temporary")
        raise RuntimeError("deterministic stream AI failure")

    monkeypatch.setattr("app.main.check_daily_quota", tracked_check)
    monkeypatch.setattr("app.main.async_resource_slot", tracked_slot)
    monkeypatch.setattr("app.main.consume_daily_quota", tracked_consume)
    monkeypatch.setattr("app.main.analyze_text_streaming", failing_stream)
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/analyze-english/stream",
            headers=_auth_headers(token),
            json=_analyze_payload("stream-failure"),
        )
    assert response.status_code == 200
    assert _quota_count(user_id) == 1
    assert events == ["quota_precheck", "slot_acquired", "quota_consume", "analyze_start", "slot_released"]


def test_postgresql_non_stream_ai_call_order(monkeypatch):
    user_id, token = _create_user("pg-quota-order")
    events: list[str] = []

    original_check = check_daily_quota
    original_consume = consume_daily_quota

    def tracked_check(*args, **kwargs):
        events.append("quota_precheck")
        return original_check(*args, **kwargs)

    @asynccontextmanager
    async def tracked_slot(*args, **kwargs):
        events.append("slot_acquired")
        try:
            yield
        finally:
            events.append("slot_released")

    def tracked_consume(*args, **kwargs):
        events.append("quota_consume")
        return original_consume(*args, **kwargs)

    def tracked_analyze(**kwargs):
        events.append("analyze_start")
        return {
            "ok": True,
            "level": "pass",
            "category": "unknown",
            "normalizedText": "quota boundary experiment non-stream-order",
            "warnings": [],
            "errors": [],
            "provider": "mock",
        }

    monkeypatch.setattr("app.main.check_daily_quota", tracked_check)
    monkeypatch.setattr("app.main.async_resource_slot", tracked_slot)
    monkeypatch.setattr("app.main.consume_daily_quota", tracked_consume)
    monkeypatch.setattr("app.main.analyze_text", tracked_analyze)

    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/analyze-english",
            headers=_auth_headers(token),
            json=_analyze_payload("non-stream-order"),
        )

    assert response.status_code == 200, response.text
    assert events == ["quota_precheck", "slot_acquired", "quota_consume", "analyze_start", "slot_released"]


def test_postgresql_stream_ai_slot_failure_does_not_increment(monkeypatch):
    user_id, token = _create_user("pg-quota-stream-slot-failure")

    @asynccontextmanager
    async def rejected_slot(*args, **kwargs):
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="slot full")
        yield

    monkeypatch.setattr("app.main.async_resource_slot", rejected_slot)
    before_count = _quota_count(user_id)
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/analyze-english/stream",
            headers=_auth_headers(token),
            json=_analyze_payload("stream-slot-failure"),
        )
    assert response.status_code == 503, response.text
    after_count = _quota_count(user_id)
    assert before_count == 0
    assert after_count == 0


def test_postgresql_stream_ai_call_order(monkeypatch):
    user_id, token = _create_user("pg-quota-stream-order")
    events: list[str] = []

    original_check = check_daily_quota
    original_consume = consume_daily_quota

    def tracked_check(*args, **kwargs):
        events.append("quota_precheck")
        return original_check(*args, **kwargs)

    @asynccontextmanager
    async def tracked_slot(*args, **kwargs):
        events.append("slot_acquired")
        try:
            yield
        finally:
            events.append("slot_released")

    def tracked_consume(*args, **kwargs):
        events.append("quota_consume")
        return original_consume(*args, **kwargs)

    def tracked_stream(**kwargs):
        events.append("analyze_start")
        yield ("final", {
            "ok": True,
            "level": "pass",
            "category": "unknown",
            "normalizedText": "quota boundary experiment stream-order",
            "warnings": [],
            "errors": [],
            "provider": "mock",
        })

    monkeypatch.setattr("app.main.check_daily_quota", tracked_check)
    monkeypatch.setattr("app.main.async_resource_slot", tracked_slot)
    monkeypatch.setattr("app.main.consume_daily_quota", tracked_consume)
    monkeypatch.setattr("app.main.analyze_text_streaming", tracked_stream)

    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/analyze-english/stream",
            headers=_auth_headers(token),
            json=_analyze_payload("stream-order"),
        )

    assert response.status_code == 200, response.text
    assert events == ["quota_precheck", "slot_acquired", "quota_consume", "analyze_start", "slot_released"]


def test_postgresql_concurrent_quota_consumption_is_atomic():
    user_id, _ = _create_user("pg-quota-concurrent")
    limit = 4

    def consume() -> str:
        try:
            with SessionLocal() as db:
                consume_daily_quota(db, user_id=user_id, resource="ai", limit=limit)
            return "success"
        except Exception as exc:  # quota rejection is an expected race outcome
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(8)))

    assert outcomes.count("success") == limit
    with SessionLocal() as db:
        rows = list(
            db.scalars(
                select(ResourceUsage).where(
                    ResourceUsage.user_id == user_id,
                    ResourceUsage.resource == "ai",
                    ResourceUsage.usage_date == date.today(),
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].count == limit


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def _review_session_values(user_id: UUID, **overrides) -> dict:
    values = {
        "user_id": user_id,
        "review_date": date.today(),
        "timezone": "UTC",
        "session_type": "daily_suggested",
        "started_at": datetime.now(timezone.utc),
        "status": "active",
        "batch_size": 5,
        "total_count": 0,
        "reviewed_count": 0,
        "completed_count": 0,
        "planned_new_count": 0,
        "planned_review_count": 0,
        "current_index": 0,
    }
    values.update(overrides)
    return values


def _create_constraint_review_context(prefix: str) -> tuple[UUID, UUID, UUID, UUID]:
    user_id, _ = _create_user(prefix)
    card_id = _create_review_ready_card(user_id, f"{prefix} card")
    with SessionLocal() as db:
        session = ReviewSession(
            **_review_session_values(
                user_id,
                total_count=1,
                planned_new_count=1,
            )
        )
        db.add(session)
        db.flush()
        item = ReviewSessionItem(
            session_id=session.id,
            card_id=card_id,
            position=0,
            status="pending",
            reappear_count=0,
            is_repeat=False,
            repeat_count=0,
        )
        db.add(item)
        db.commit()
        return user_id, card_id, session.id, item.id


def _review_log_values(
    user_id: UUID,
    card_id: UUID,
    session_id: UUID,
    session_item_id: UUID,
    **overrides,
) -> dict:
    values = {
        "user_id": user_id,
        "card_id": card_id,
        "session_id": session_id,
        "session_item_id": session_item_id,
        "session_type": "daily_suggested",
        "result": "got_it",
        "reviewed_at": datetime.now(timezone.utc),
        "card_state_before_review": "new",
        "review_state_before": "new",
        "review_state_after": "reviewing",
        "mastery_score_before": 0,
        "mastery_score_after": 1,
        "recovery_stage_before": 0,
        "recovery_stage_after": 0,
    }
    values.update(overrides)
    return values


def test_postgresql_a_class_constraints_allow_valid_direct_writes():
    user_id, card_id, session_id, session_item_id = _create_constraint_review_context("pg-a-valid")

    with SessionLocal() as db:
        usage = ResourceUsage(
            user_id=user_id,
            resource="lexical",
            usage_date=date.today(),
            count=0,
        )
        log = ReviewLog(
            **_review_log_values(user_id, card_id, session_id, session_item_id)
        )
        db.add_all((usage, log))
        db.commit()
        assert usage.id is not None
        assert log.id is not None


def test_postgresql_resource_usage_constraints_reject_invalid_direct_writes():
    invalid_cases = (
        ({"count": -1}, "ck_resource_usage_count_nonnegative"),
        ({"resource": "unknown_resource"}, "ck_resource_usage_resource"),
    )
    for values, expected_constraint in invalid_cases:
        user_id, _ = _create_user(f"pg-usage-constraint-{uuid4()}")
        with SessionLocal() as db:
            resource_usage_values = {
                "user_id": user_id,
                "resource": "ai",
                "usage_date": date.today(),
                "count": 0,
            }
            resource_usage_values.update(values)
            db.add(ResourceUsage(**resource_usage_values))
            with pytest.raises(IntegrityError) as exc_info:
                db.flush()
            assert _constraint_name(exc_info.value) == expected_constraint
            db.rollback()


def test_postgresql_review_session_count_constraints_reject_negative_direct_writes():
    invalid_cases = (
        ("total_count", "ck_review_sessions_total_count_nonnegative"),
        ("completed_count", "ck_review_sessions_completed_count_nonnegative"),
        ("reviewed_count", "ck_review_sessions_reviewed_count_nonnegative"),
        ("current_index", "ck_review_sessions_current_index_nonnegative"),
        ("planned_new_count", "ck_review_sessions_planned_new_count_nonnegative"),
        ("planned_review_count", "ck_review_sessions_planned_review_count_nonnegative"),
    )
    for field, expected_constraint in invalid_cases:
        user_id, _ = _create_user(f"pg-session-constraint-{field}-{uuid4()}")
        with SessionLocal() as db:
            db.add(ReviewSession(**_review_session_values(user_id, **{field: -1})))
            with pytest.raises(IntegrityError) as exc_info:
                db.flush()
            assert _constraint_name(exc_info.value) == expected_constraint
            db.rollback()


def test_postgresql_review_session_item_constraints_reject_invalid_direct_writes():
    invalid_cases = (
        ("position", -1, "ck_review_session_items_position_nonnegative"),
        ("repeat_count", -1, "ck_review_session_items_repeat_count_nonnegative"),
        ("reappear_count", -1, "ck_review_session_items_reappear_count_nonnegative"),
        ("result", "unknown_result", "ck_review_session_items_result"),
        ("first_result", "unknown_result", "ck_review_session_items_first_result"),
        ("final_result", "unknown_result", "ck_review_session_items_final_result"),
    )
    for field, value, expected_constraint in invalid_cases:
        user_id, card_id, session_id, _ = _create_constraint_review_context(
            f"pg-item-constraint-{field}-{uuid4()}"
        )
        with SessionLocal() as db:
            values = {
                "session_id": session_id,
                "card_id": card_id,
                "position": 1,
                "status": "pending",
                "reappear_count": 0,
                "is_repeat": False,
                "repeat_count": 0,
                field: value,
            }
            db.add(ReviewSessionItem(**values))
            with pytest.raises(IntegrityError) as exc_info:
                db.flush()
            assert _constraint_name(exc_info.value) == expected_constraint
            db.rollback()


def test_postgresql_review_log_constraints_reject_invalid_direct_writes():
    invalid_cases = (
        ("session_type", "unknown_session", "ck_review_logs_session_type"),
        ("card_state_before_review", "unknown_state", "ck_review_logs_card_state_before_review"),
        ("review_state_before", "unknown_state", "ck_review_logs_review_state_before"),
        ("review_state_after", "unknown_state", "ck_review_logs_review_state_after"),
        ("mastery_score_before", -1, "ck_review_logs_mastery_score_before_range"),
        ("mastery_score_after", 6, "ck_review_logs_mastery_score_after_range"),
        ("recovery_stage_before", -1, "ck_review_logs_recovery_stage_before_range"),
        ("recovery_stage_after", 3, "ck_review_logs_recovery_stage_after_range"),
    )
    for field, value, expected_constraint in invalid_cases:
        user_id, card_id, session_id, session_item_id = _create_constraint_review_context(
            f"pg-log-constraint-{field}-{uuid4()}"
        )
        with SessionLocal() as db:
            db.add(
                ReviewLog(
                    **_review_log_values(
                        user_id,
                        card_id,
                        session_id,
                        session_item_id,
                        **{field: value},
                    )
                )
            )
            with pytest.raises(IntegrityError) as exc_info:
                db.flush()
            assert _constraint_name(exc_info.value) == expected_constraint
            db.rollback()


def test_postgresql_review_log_session_item_unique_constraint_rejects_duplicate():
    user_id, card_id, session_id, session_item_id = _create_constraint_review_context("pg-log-unique")

    with SessionLocal() as db:
        db.add(ReviewLog(**_review_log_values(user_id, card_id, session_id, session_item_id)))
        db.commit()
        db.add(ReviewLog(**_review_log_values(user_id, card_id, session_id, session_item_id)))
        with pytest.raises(IntegrityError) as exc_info:
            db.flush()
        assert _constraint_name(exc_info.value) == "uq_review_logs_session_item_id"
        db.rollback()


def test_postgresql_one_active_session_per_user_constraint_rejects_duplicate():
    user_id, _, _, _ = _create_constraint_review_context("pg-active-session-unique")

    with SessionLocal() as db:
        db.add(ReviewSession(**_review_session_values(user_id)))
        with pytest.raises(IntegrityError) as exc_info:
            db.flush()
        assert _constraint_name(exc_info.value) == "ux_review_sessions_one_active_per_user"
        db.rollback()
