from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from app.services.review_scheduler import (
    calculate_next_review_at,
    classify_card_status,
    select_today_cards,
    should_append_repeat_item,
)


TIMEZONE = "Asia/Tokyo"
TODAY = date(2026, 5, 6)


def make_card(**overrides):
    card = {
        "id": "card-default",
        "status": "active",
        "review_count": 1,
        "again_count": 0,
        "hard_count": 0,
        "last_review_result": "good",
        "next_review_at": datetime(2026, 5, 6, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
        "created_at": datetime(2026, 5, 1, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
    }
    card.update(overrides)
    return card


class ReviewSchedulerTest(unittest.TestCase):
    def test_new_card_is_selected_for_today(self):
        cards = [make_card(id="new", review_count=0, next_review_at=None)]

        selected = select_today_cards(cards, TODAY, TIMEZONE)

        self.assertEqual(["new"], [card["id"] for card in selected])

    def test_due_card_is_selected_for_today(self):
        cards = [make_card(id="due")]

        selected = select_today_cards(cards, TODAY, TIMEZONE)

        self.assertEqual(["due"], [card["id"] for card in selected])

    def test_future_mastered_card_is_not_selected_for_today(self):
        cards = [
            make_card(
                id="future",
                review_count=3,
                last_review_result="easy",
                next_review_at=datetime(2026, 5, 10, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
            )
        ]

        selected = select_today_cards(cards, TODAY, TIMEZONE)

        self.assertEqual([], selected)

    def test_again_and_hard_cards_are_prioritized_before_regular_due_cards(self):
        cards = [
            make_card(id="regular-due", last_review_result="good"),
            make_card(id="again-due", last_review_result="again"),
            make_card(id="hard-due", last_review_result="hard"),
        ]

        selected = select_today_cards(cards, TODAY, TIMEZONE)

        self.assertEqual(
            ["again-due", "hard-due", "regular-due"],
            [card["id"] for card in selected],
        )

    def test_deleted_card_is_not_selected_for_today(self):
        cards = [make_card(id="deleted", status="deleted", last_review_result="again")]

        selected = select_today_cards(cards, TODAY, TIMEZONE)

        self.assertEqual([], selected)

    def test_again_and_hard_append_repeat_when_repeat_count_is_under_limit(self):
        for result in ("again", "hard"):
            with self.subTest(result=result):
                self.assertTrue(should_append_repeat_item(result, 0))
                self.assertTrue(should_append_repeat_item(result, 1))

    def test_repeat_count_at_limit_does_not_append_repeat(self):
        for result in ("again", "hard"):
            with self.subTest(result=result):
                self.assertFalse(should_append_repeat_item(result, 2))
                self.assertFalse(should_append_repeat_item(result, 3))

    def test_good_and_easy_do_not_append_repeat(self):
        for result in ("good", "easy"):
            with self.subTest(result=result):
                self.assertFalse(should_append_repeat_item(result, 0))

    def test_calculate_next_review_at_for_all_results(self):
        reviewed_at = datetime(2026, 5, 6, 20, 30, tzinfo=ZoneInfo(TIMEZONE))

        expected_dates = {
            "again": date(2026, 5, 7),
            "hard": date(2026, 5, 7),
            "good": date(2026, 5, 9),
            "easy": date(2026, 5, 13),
        }

        for result, expected_date in expected_dates.items():
            with self.subTest(result=result):
                next_review_at = calculate_next_review_at({}, result, reviewed_at, TIMEZONE)
                self.assertEqual(expected_date, next_review_at.date())
                self.assertEqual(20, next_review_at.hour)
                self.assertEqual(30, next_review_at.minute)
                self.assertEqual(ZoneInfo(TIMEZONE), next_review_at.tzinfo)

    def test_classify_card_status_due_weak_and_mastered(self):
        due_card = make_card(id="due", review_count=0, next_review_at=None)
        weak_card = make_card(
            id="weak",
            last_review_result="good",
            again_count=2,
            next_review_at=datetime(2026, 5, 10, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
        )
        mastered_card = make_card(
            id="mastered",
            review_count=4,
            last_review_result="easy",
            next_review_at=datetime(2026, 5, 10, 9, 0, tzinfo=ZoneInfo(TIMEZONE)),
        )

        self.assertEqual("due", classify_card_status(due_card, TODAY, TIMEZONE))
        self.assertEqual("weak", classify_card_status(weak_card, TODAY, TIMEZONE))
        self.assertEqual("mastered", classify_card_status(mastered_card, TODAY, TIMEZONE))


if __name__ == "__main__":
    unittest.main()
