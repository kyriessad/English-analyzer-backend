"""
Phase 2 review scheduling rules.
"""
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.card import Card


VALID_LIMITS = {5, 10, 15}
VALID_RESULTS = {"forgot", "shaky", "got_it", "fluent"}
REVIEW_STATES = {"new", "strengthening", "reviewing", "mastered"}
BASE_INTERVAL_DAYS = {
    0: 1,
    1: 2,
    2: 4,
    3: 7,
    4: 14,
    5: 30,
}
RESULT_COUNT_FIELDS = {
    "forgot": "forgot_count",
    "shaky": "shaky_count",
    "got_it": "got_it_count",
    "fluent": "fluent_count",
}


def _get_value(card: Any, field: str, default: Any = None) -> Any:
    if isinstance(card, dict):
        return card.get(field, default)
    return getattr(card, field, default)


def _get_int(card: Any, field: str, default: int = 0) -> int:
    value = _get_value(card, field, default)
    if value is None:
        return default
    return int(value)


def _card_id(card: Any) -> str:
    return str(_get_value(card, "id", ""))


def _clamp_score(score: int) -> int:
    return max(0, min(int(score or 0), 5))


def _clamp_recovery_stage(stage: int) -> int:
    return max(0, min(int(stage or 0), 2))


def _validate_result(result: str) -> None:
    if result not in VALID_RESULTS:
        raise ValueError(f"Unsupported review result: {result}")


def _as_sort_datetime(value: Any, now: datetime) -> datetime:
    if value is None:
        return datetime.max.replace(tzinfo=now.tzinfo)

    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is None and now.tzinfo is not None:
        return parsed.replace(tzinfo=now.tzinfo)
    if parsed.tzinfo is not None and now.tzinfo is not None:
        return parsed.astimezone(now.tzinfo)
    if parsed.tzinfo is not None and now.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed


def _is_due(card: Any, now: datetime) -> bool:
    next_review_at = _get_value(card, "next_review_at")
    if next_review_at is None:
        return False
    return _as_sort_datetime(next_review_at, now) <= now


def normalize_review_limit(limit: Any) -> int:
    try:
        normalized = int(limit)
    except (TypeError, ValueError):
        return 5
    return normalized if normalized in VALID_LIMITS else 5


def get_new_quota(limit: int) -> int:
    normalized_limit = normalize_review_limit(limit)
    return {5: 1, 10: 2, 15: 3}[normalized_limit]


def calculate_effective_new_quota(limit: int, strengthening_count: int, due_count: int) -> int:
    normalized_limit = normalize_review_limit(limit)
    new_quota = get_new_quota(normalized_limit)

    if strengthening_count >= normalized_limit * 2:
        return 0

    if due_count >= normalized_limit * 2:
        return max(0, new_quota // 2)

    return new_quota


def calculate_mastery_score_after_feedback(current_score: int, result: str) -> int:
    _validate_result(result)
    score = _clamp_score(current_score)

    if result == "forgot":
        return {0: 0, 1: 0, 2: 0, 3: 1, 4: 2, 5: 3}[score]

    if result == "shaky":
        return {0: 0, 1: 1, 2: 2, 3: 2, 4: 3, 5: 4}[score]

    if result == "got_it":
        return min(score + 1, 5)

    return min(score + 2, 5)


def calculate_recovery_stage_after_feedback(current_recovery_stage: int, result: str) -> int:
    _validate_result(result)
    recovery_stage = _clamp_recovery_stage(current_recovery_stage)

    if result == "forgot":
        return 2

    if result == "shaky":
        return 1

    return max(recovery_stage - 1, 0)


def calculate_review_state_after_feedback(
    result: str,
    mastery_score_after: int,
    recovery_stage_after: int,
) -> str:
    _validate_result(result)

    if result in {"forgot", "shaky"}:
        return "strengthening"

    if result == "got_it":
        return "reviewing"

    if mastery_score_after >= 5 and recovery_stage_after == 0:
        return "mastered"

    return "reviewing"


def calculate_next_review_at(
    result: str,
    mastery_score_after: int,
    recovery_stage_before: int,
    now: datetime,
) -> datetime:
    _validate_result(result)

    if result == "forgot":
        return now + timedelta(days=1)

    if result == "shaky":
        return now + timedelta(days=2)

    interval_days = BASE_INTERVAL_DAYS[_clamp_score(mastery_score_after)]
    recovery_stage = _clamp_recovery_stage(recovery_stage_before)

    if recovery_stage == 2:
        interval_days = min(interval_days, 7)
    elif recovery_stage == 1:
        interval_days = min(interval_days, 14)

    return now + timedelta(days=interval_days)


def should_append_reappear_item(result: str, current_reappear_count: int) -> bool:
    _validate_result(result)
    reappear_count = max(int(current_reappear_count or 0), 0)

    if result == "forgot":
        return reappear_count < 2

    if result == "shaky":
        return reappear_count < 1

    return False


def calculate_reappear_insert_position(current_position: int, current_max_position: int) -> int:
    return min(int(current_position) + 5, int(current_max_position) + 1)


def apply_review_feedback_to_card(card: Card, result: str, now: datetime) -> dict[str, Any]:
    _validate_result(result)

    review_state_before = card.review_state or "new"
    mastery_score_before = _clamp_score(card.mastery_score)
    recovery_stage_before = _clamp_recovery_stage(card.recovery_stage)
    next_review_at_before = card.next_review_at

    mastery_score_after = calculate_mastery_score_after_feedback(mastery_score_before, result)
    recovery_stage_after = calculate_recovery_stage_after_feedback(recovery_stage_before, result)
    review_state_after = calculate_review_state_after_feedback(
        result,
        mastery_score_after,
        recovery_stage_after,
    )
    if review_state_before == "new" and result == "fluent":
        review_state_after = "reviewing"
    next_review_at_after = calculate_next_review_at(
        result,
        mastery_score_after,
        recovery_stage_before,
        now,
    )

    card.review_state = review_state_after
    card.mastery_score = mastery_score_after
    card.recovery_stage = recovery_stage_after
    card.review_count = max(int(card.review_count or 0), 0) + 1
    if card.first_reviewed_at is None:
        card.first_reviewed_at = now
    card.last_reviewed_at = now
    card.last_review_result = result
    card.next_review_at = next_review_at_after

    count_field = RESULT_COUNT_FIELDS[result]
    setattr(card, count_field, max(int(getattr(card, count_field) or 0), 0) + 1)

    return {
        "review_state_before": review_state_before,
        "review_state_after": review_state_after,
        "mastery_score_before": mastery_score_before,
        "mastery_score_after": mastery_score_after,
        "recovery_stage_before": recovery_stage_before,
        "recovery_stage_after": recovery_stage_after,
        "next_review_at_before": next_review_at_before,
        "next_review_at_after": next_review_at_after,
    }


def get_due_reason(card: Any, now: datetime) -> str | None:
    review_state = _get_value(card, "review_state", "new") or "new"
    recovery_stage = _get_int(card, "recovery_stage", 0)

    if review_state == "new":
        return "new"

    if review_state == "strengthening":
        return "strengthening"

    if not _is_due(card, now):
        return None

    if recovery_stage > 0 and review_state in {"reviewing", "mastered"}:
        return "recovery_due"

    if review_state == "reviewing" and recovery_stage == 0:
        return "reviewing_due"

    if review_state == "mastered" and recovery_stage == 0:
        return "mastered_due"

    return None


def sort_strengthening_cards(cards: list[Any], now: datetime) -> list[Any]:
    result_priority = {"forgot": 0, "shaky": 1}

    return sorted(
        cards,
        key=lambda card: (
            0 if _is_due(card, now) else 1,
            -_get_int(card, "recovery_stage", 0),
            result_priority.get(_get_value(card, "last_review_result"), 2),
            _as_sort_datetime(_get_value(card, "next_review_at"), now),
            _as_sort_datetime(_get_value(card, "last_reviewed_at"), now),
            _as_sort_datetime(_get_value(card, "created_at"), now),
            _card_id(card),
        ),
    )


def sort_due_cards(cards: list[Any], now: datetime) -> list[Any]:
    due_priority = {
        "recovery_due": 0,
        "reviewing_due": 1,
        "mastered_due": 2,
    }

    return sorted(
        cards,
        key=lambda card: (
            due_priority.get(get_due_reason(card, now), 3),
            _as_sort_datetime(_get_value(card, "next_review_at"), now),
            -_get_int(card, "recovery_stage", 0),
            _as_sort_datetime(_get_value(card, "last_reviewed_at"), now),
            _as_sort_datetime(_get_value(card, "created_at"), now),
            _card_id(card),
        ),
    )


def sort_new_cards(cards: list[Any]) -> list[Any]:
    now = datetime.now()
    return sorted(
        cards,
        key=lambda card: (
            _as_sort_datetime(_get_value(card, "created_at"), now),
            _card_id(card),
        ),
    )


def _dedupe_cards(cards: list[Card]) -> list[Card]:
    seen_ids: set[str] = set()
    deduped: list[Card] = []

    for card in cards:
        card_id = _card_id(card)
        if card_id in seen_ids:
            continue
        seen_ids.add(card_id)
        deduped.append(card)

    return deduped


def select_review_cards(user_id: UUID, limit: int, now: datetime, db: Session) -> list[Card]:
    normalized_limit = normalize_review_limit(limit)
    candidate_cards = list(
        db.scalars(
            select(Card).where(
                Card.user_id == user_id,
                Card.deleted_at.is_(None),
                Card.status == "active",
                Card.review_state.in_(tuple(REVIEW_STATES)),
                Card.is_review_ready.is_(True),
                Card.analysis_status != "pending",
                Card.needs_manual_fix.is_(False),
                or_(
                    Card.review_state == "new",
                    Card.review_state == "strengthening",
                    Card.next_review_at <= now,
                ),
            )
        )
    )

    candidate_cards = _dedupe_cards(candidate_cards)
    strengthening_cards = sort_strengthening_cards(
        [card for card in candidate_cards if get_due_reason(card, now) == "strengthening"],
        now,
    )
    due_cards = sort_due_cards(
        [
            card
            for card in candidate_cards
            if get_due_reason(card, now) in {"recovery_due", "reviewing_due", "mastered_due"}
        ],
        now,
    )
    new_cards = sort_new_cards(
        [card for card in candidate_cards if get_due_reason(card, now) == "new"]
    )

    effective_new_quota = calculate_effective_new_quota(
        normalized_limit,
        len(strengthening_cards),
        len(due_cards),
    )

    selected_new = new_cards[:effective_new_quota]
    selected_new_ids = {_card_id(card) for card in selected_new}
    remaining_slots = normalized_limit - len(selected_new)

    selected_old = (strengthening_cards + due_cards)[:remaining_slots]
    selected_old_ids = {_card_id(card) for card in selected_old}

    selected = selected_old + selected_new

    if len(selected) < normalized_limit:
        selected.extend(
            card
            for card in new_cards
            if _card_id(card) not in selected_new_ids and _card_id(card) not in selected_old_ids
        )

    return _dedupe_cards(selected)[:normalized_limit]
