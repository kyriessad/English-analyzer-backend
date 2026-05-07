"""
Pure review scheduling rules.
"""
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


REPEATABLE_RESULTS = {"again", "hard"}
NEXT_REVIEW_DAY_OFFSETS = {
    "again": 1,
    "hard": 1,
    "good": 3,
    "easy": 7,
}
MAX_REPEAT_COUNT_PER_ROUND = 2
WEAK_COUNT_THRESHOLD = 2


def _get_value(card: Any, field: str, default: Any = None) -> Any:
    if isinstance(card, dict):
        return card.get(field, default)
    return getattr(card, field, default)


def _get_int(card: Any, field: str, default: int = 0) -> int:
    value = _get_value(card, field, default)
    if value is None:
        return default
    return int(value)


def _get_timezone(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(timezone_name or "UTC")


def _parse_iso_datetime(value: str, timezone: ZoneInfo) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed_date = date.fromisoformat(normalized)
        return datetime.combine(parsed_date, time.min, tzinfo=timezone)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _as_local_datetime(value: Any, timezone: ZoneInfo) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)

    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone)

    if isinstance(value, str):
        return _parse_iso_datetime(value, timezone)

    raise TypeError(f"Unsupported datetime value: {value!r}")


def _as_local_date(value: Any, timezone: ZoneInfo) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    local_datetime = _as_local_datetime(value, timezone)
    if local_datetime is None:
        raise ValueError("Date value is required")
    return local_datetime.date()


def _is_active(card: Any) -> bool:
    return _get_value(card, "status", "active") == "active"


def _is_new_card(card: Any) -> bool:
    return _get_int(card, "review_count", 0) == 0


def _is_weak_card(card: Any) -> bool:
    last_review_result = _get_value(card, "last_review_result")
    if last_review_result in REPEATABLE_RESULTS:
        return True

    return (
        _get_int(card, "again_count", 0) >= WEAK_COUNT_THRESHOLD
        or _get_int(card, "hard_count", 0) >= WEAK_COUNT_THRESHOLD
    )


def _is_due_card(card: Any, review_date: date, timezone: ZoneInfo) -> bool:
    if _is_new_card(card):
        return True

    next_review_at = _as_local_datetime(_get_value(card, "next_review_at"), timezone)
    return next_review_at is not None and next_review_at.date() <= review_date


def _stable_card_id(card: Any) -> str:
    for field in ("id", "local_temp_id", "legacy_cloud_id", "content"):
        value = _get_value(card, field)
        if value is not None:
            return str(value)
    return ""


def _selection_sort_key(card_with_index: tuple[int, Any], review_date: date, timezone: ZoneInfo) -> tuple[Any, ...]:
    original_index, card = card_with_index
    next_review_at = _as_local_datetime(_get_value(card, "next_review_at"), timezone)
    created_at = _as_local_datetime(_get_value(card, "created_at"), timezone)

    if _is_weak_card(card):
        priority = 0
    elif _is_new_card(card):
        priority = 1
    else:
        priority = 2

    return (
        priority,
        next_review_at.date() if next_review_at else date.max,
        created_at.date() if created_at else date.max,
        _stable_card_id(card),
        original_index,
    )


def select_today_cards(cards: list[Any], review_date: Any, timezone: str) -> list[Any]:
    """
    Select active cards that should appear in today's review task.

    The function is intentionally side-effect free and returns the original card
    objects in a deterministic order.
    """
    local_timezone = _get_timezone(timezone)
    local_review_date = _as_local_date(review_date, local_timezone)

    selected_cards = [
        (index, card)
        for index, card in enumerate(cards)
        if _is_active(card)
        and (
            _is_due_card(card, local_review_date, local_timezone)
            or _is_weak_card(card)
        )
    ]

    selected_cards.sort(
        key=lambda item: _selection_sort_key(item, local_review_date, local_timezone)
    )
    return [card for _, card in selected_cards]


def calculate_next_review_at(card: Any, result: str, reviewed_at: Any, timezone: str) -> datetime:
    """
    Calculate the next review datetime using the first-version spacing rules.
    """
    if result not in NEXT_REVIEW_DAY_OFFSETS:
        raise ValueError(f"Unsupported review result: {result}")

    local_timezone = _get_timezone(timezone)
    local_reviewed_at = _as_local_datetime(reviewed_at, local_timezone)
    if local_reviewed_at is None:
        raise ValueError("reviewed_at is required")

    return local_reviewed_at + timedelta(days=NEXT_REVIEW_DAY_OFFSETS[result])


def should_append_repeat_item(result: str, repeat_count: int) -> bool:
    """
    Decide whether the current feedback should append a repeat item to this round.
    """
    return result in REPEATABLE_RESULTS and repeat_count < MAX_REPEAT_COUNT_PER_ROUND


def classify_card_status(card: Any, today: Any, timezone: str) -> str:
    """
    Return the card's primary homepage filter bucket.
    """
    local_timezone = _get_timezone(timezone)
    local_today = _as_local_date(today, local_timezone)

    if not _is_active(card):
        return "all"

    if _is_weak_card(card):
        return "weak"

    if _is_due_card(card, local_today, local_timezone):
        return "due"

    if _get_int(card, "review_count", 0) > 0:
        return "mastered"

    return "all"
