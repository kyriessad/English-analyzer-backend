import unittest

from fastapi.testclient import TestClient

from app.main import app


TIMEZONE = "Asia/Shanghai"


def make_card(**overrides):
    card = {
        "id": "card-default",
        "status": "active",
        "review_count": 1,
        "again_count": 0,
        "hard_count": 0,
        "last_review_result": "good",
        "next_review_at": "2026-05-06T09:00:00+08:00",
        "created_at": "2026-05-01T09:00:00+08:00",
    }
    card.update(overrides)
    return card


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_preview_today_returns_new_card_but_excludes_deleted_and_future_mastered(self):
        response = self.client.post(
            "/api/review/preview-today",
            json={
                "review_date": "2026-05-06",
                "timezone": TIMEZONE,
                "cards": [
                    make_card(id="new", review_count=0, next_review_at=None),
                    make_card(
                        id="deleted",
                        status="deleted",
                        review_count=0,
                        next_review_at=None,
                    ),
                    make_card(
                        id="future-mastered",
                        review_count=5,
                        last_review_result="easy",
                        next_review_at="2026-05-10T09:00:00+08:00",
                    ),
                ],
            },
        )

        self.assertEqual(200, response.status_code)
        data = response.json()
        self.assertEqual(["new"], [item["card"]["id"] for item in data["items"]])
        self.assertEqual("due", data["items"][0]["bucket"])

    def test_calculate_next_review_again_returns_tomorrow(self):
        response = self.client.post(
            "/api/review/calculate-next-review",
            json={
                "card": make_card(id="again"),
                "result": "again",
                "reviewed_at": "2026-05-06T20:30:00+08:00",
                "timezone": TIMEZONE,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["next_review_at"].startswith("2026-05-07T20:30:00"))

    def test_calculate_next_review_good_returns_three_days_later(self):
        response = self.client.post(
            "/api/review/calculate-next-review",
            json={
                "card": make_card(id="good"),
                "result": "good",
                "reviewed_at": "2026-05-06T20:30:00+08:00",
                "timezone": TIMEZONE,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["next_review_at"].startswith("2026-05-09T20:30:00"))

    def test_classify_card_returns_weak_when_again_count_is_high(self):
        response = self.client.post(
            "/api/review/classify-card",
            json={
                "today": "2026-05-06",
                "timezone": TIMEZONE,
                "card": make_card(
                    id="weak",
                    again_count=2,
                    next_review_at="2026-05-10T09:00:00+08:00",
                ),
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("weak", response.json()["bucket"])

    def test_classify_card_returns_mastered_for_future_good_card(self):
        response = self.client.post(
            "/api/review/classify-card",
            json={
                "today": "2026-05-06",
                "timezone": TIMEZONE,
                "card": make_card(
                    id="mastered",
                    review_count=5,
                    last_review_result="easy",
                    next_review_at="2026-05-10T09:00:00+08:00",
                ),
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("mastered", response.json()["bucket"])


if __name__ == "__main__":
    unittest.main()
