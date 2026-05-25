from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from app.services.review_rules import (
    apply_review_feedback_to_card,
    calculate_effective_new_quota,
    calculate_mastery_score_after_feedback,
    calculate_next_review_at,
    calculate_reappear_insert_position,
    calculate_recovery_stage_after_feedback,
    calculate_review_state_after_feedback,
    get_due_reason,
    get_new_quota,
    normalize_review_limit,
    should_append_reappear_item,
    sort_due_cards,
    sort_new_cards,
    sort_strengthening_cards,
)


NOW = datetime(2026, 5, 8, 9, 30, tzinfo=timezone.utc)


def make_card(**overrides):
    values = {
        "id": "card",
        "review_state": "reviewing",
        "mastery_score": 0,
        "recovery_stage": 0,
        "last_review_result": None,
        "next_review_at": NOW - timedelta(days=1),
        "last_reviewed_at": NOW - timedelta(days=3),
        "created_at": NOW - timedelta(days=10),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ReviewRulesTest(unittest.TestCase):
    def test_normalize_review_limit_allows_only_configured_batch_sizes(self):
        self.assertEqual(5, normalize_review_limit(None))
        self.assertEqual(5, normalize_review_limit("bad"))
        self.assertEqual(5, normalize_review_limit(7))
        self.assertEqual(10, normalize_review_limit("10"))
        self.assertEqual(15, normalize_review_limit(15))

    def test_new_quota_and_effective_new_quota(self):
        self.assertEqual(1, get_new_quota(5))
        self.assertEqual(2, get_new_quota(10))
        self.assertEqual(3, get_new_quota(15))
        self.assertEqual(0, calculate_effective_new_quota(5, strengthening_count=10, due_count=0))
        self.assertEqual(1, calculate_effective_new_quota(10, strengthening_count=0, due_count=20))
        self.assertEqual(3, calculate_effective_new_quota(15, strengthening_count=0, due_count=0))

    def test_mastery_score_after_feedback(self):
        self.assertEqual(0, calculate_mastery_score_after_feedback(2, "forgot"))
        self.assertEqual(2, calculate_mastery_score_after_feedback(3, "shaky"))
        self.assertEqual(5, calculate_mastery_score_after_feedback(4, "got_it"))
        self.assertEqual(5, calculate_mastery_score_after_feedback(4, "fluent"))
        with self.assertRaises(ValueError):
            calculate_mastery_score_after_feedback(1, "again")

    def test_recovery_stage_and_review_state_after_feedback(self):
        self.assertEqual(2, calculate_recovery_stage_after_feedback(0, "forgot"))
        self.assertEqual(1, calculate_recovery_stage_after_feedback(0, "shaky"))
        self.assertEqual(1, calculate_recovery_stage_after_feedback(2, "got_it"))
        self.assertEqual("strengthening", calculate_review_state_after_feedback("forgot", 0, 2))
        self.assertEqual("reviewing", calculate_review_state_after_feedback("got_it", 4, 0))
        self.assertEqual("mastered", calculate_review_state_after_feedback("fluent", 5, 0))
        self.assertEqual("reviewing", calculate_review_state_after_feedback("fluent", 5, 1))

    def test_next_review_uses_recovery_stage_before_for_got_it_and_fluent(self):
        self.assertEqual(NOW + timedelta(days=1), calculate_next_review_at("forgot", 5, 0, NOW))
        self.assertEqual(NOW + timedelta(days=2), calculate_next_review_at("shaky", 5, 0, NOW))
        self.assertEqual(NOW + timedelta(days=7), calculate_next_review_at("fluent", 5, 2, NOW))
        self.assertEqual(NOW + timedelta(days=14), calculate_next_review_at("fluent", 5, 1, NOW))
        self.assertEqual(NOW + timedelta(days=30), calculate_next_review_at("fluent", 5, 0, NOW))

    def test_reappear_rules_and_position(self):
        self.assertTrue(should_append_reappear_item("forgot", 0))
        self.assertTrue(should_append_reappear_item("forgot", 1))
        self.assertFalse(should_append_reappear_item("forgot", 2))
        self.assertTrue(should_append_reappear_item("shaky", 0))
        self.assertFalse(should_append_reappear_item("shaky", 1))
        self.assertFalse(should_append_reappear_item("got_it", 0))
        self.assertEqual(8, calculate_reappear_insert_position(3, 20))
        self.assertEqual(5, calculate_reappear_insert_position(3, 4))

    def test_due_reason(self):
        self.assertEqual("new", get_due_reason(make_card(review_state="new"), NOW))
        self.assertEqual("strengthening", get_due_reason(make_card(review_state="strengthening"), NOW))
        self.assertEqual(
            "recovery_due",
            get_due_reason(make_card(review_state="reviewing", recovery_stage=1), NOW),
        )
        self.assertEqual("reviewing_due", get_due_reason(make_card(review_state="reviewing"), NOW))
        self.assertEqual("mastered_due", get_due_reason(make_card(review_state="mastered"), NOW))
        self.assertIsNone(
            get_due_reason(make_card(review_state="reviewing", next_review_at=NOW + timedelta(days=1)), NOW)
        )

    def test_sorting_rules(self):
        strengthening = [
            make_card(id="not-due", review_state="strengthening", recovery_stage=2, next_review_at=NOW + timedelta(days=1)),
            make_card(id="forgot", review_state="strengthening", recovery_stage=2, last_review_result="forgot"),
            make_card(id="shaky", review_state="strengthening", recovery_stage=2, last_review_result="shaky"),
        ]
        self.assertEqual(["forgot", "shaky", "not-due"], [card.id for card in sort_strengthening_cards(strengthening, NOW)])

        due = [
            make_card(id="mastered", review_state="mastered"),
            make_card(id="reviewing", review_state="reviewing"),
            make_card(id="recovery", review_state="reviewing", recovery_stage=1),
        ]
        self.assertEqual(["recovery", "reviewing", "mastered"], [card.id for card in sort_due_cards(due, NOW)])

        new_cards = [
            make_card(id="newer", review_state="new", created_at=NOW),
            make_card(id="older", review_state="new", created_at=NOW - timedelta(days=1)),
        ]
        self.assertEqual(["older", "newer"], [card.id for card in sort_new_cards(new_cards)])


class RepeatItemMasteredCapTest(unittest.TestCase):
    """Phase 8J: repeat item that failed cannot restore mastered in the same round."""

    def test_repeat_fluent_after_shaky_capped_to_reviewing(self):
        # core case: mastered card, shaky first → reappear → fluent → reviewing, NOT mastered
        state = calculate_review_state_after_feedback(
            "fluent", 5, 0,
            is_reappear=True,
            first_failed_result="shaky",
        )
        self.assertEqual("reviewing", state)

    def test_repeat_fluent_after_forgot_capped_to_reviewing(self):
        state = calculate_review_state_after_feedback(
            "fluent", 5, 0,
            is_reappear=True,
            first_failed_result="forgot",
        )
        self.assertEqual("reviewing", state)

    def test_non_repeat_fluent_still_reaches_mastered(self):
        # default params: non-repeat item must still be able to reach mastered
        state = calculate_review_state_after_feedback("fluent", 5, 0)
        self.assertEqual("mastered", state)

    def test_repeat_fluent_after_got_it_not_blocked(self):
        # first_failed_result not in {forgot, shaky} → cap does not apply
        state = calculate_review_state_after_feedback(
            "fluent", 5, 0,
            is_reappear=True,
            first_failed_result="got_it",
        )
        self.assertEqual("mastered", state)

    def test_apply_feedback_repeat_shaky_fluent_caps_state_not_score(self):
        # integration: apply_review_feedback_to_card passes through the cap;
        # mastery_score and recovery_stage are NOT affected by the cap
        card = make_card(
            review_state="mastered",
            mastery_score=5,
            recovery_stage=0,
            review_count=5,
            first_reviewed_at=NOW - timedelta(days=7),
            fluent_count=3,
        )
        transitions = apply_review_feedback_to_card(
            card, "fluent", NOW,
            is_reappear=True,
            first_failed_result="shaky",
        )
        self.assertEqual("reviewing", transitions["review_state_after"])
        self.assertEqual(5, transitions["mastery_score_after"])
        self.assertEqual(0, transitions["recovery_stage_after"])
        self.assertEqual("reviewing", card.review_state)
        self.assertEqual(5, card.mastery_score)


if __name__ == "__main__":
    unittest.main()
