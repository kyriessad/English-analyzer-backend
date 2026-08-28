from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.core.config import settings as app_settings
from app.database import Base, get_db
from app.main import app
from app.models.card import Card, CardLexicalMetadata
from app.models.review import CardFsrsState, ReviewAnswerLog, ReviewMcqQuestion, ReviewSessionItem
from app.models.user import User
from app.services import auth_service
from app.services.ecdict_service import EcdictEntry
from scripts import backfill_card_lexical_metadata


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


class ReviewV1ApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="review-v1-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=30,
        )
        self.user_uuid = uuid4()
        with TestingSessionLocal() as db:
            db.add(User(id=self.user_uuid, wx_openid=f"openid-{self.user_uuid}", timezone="UTC"))
            db.commit()
        self.token = auth_service.create_access_token(self.user_uuid)
        self.card_sequence = 0

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def create_card(self, content: str, understanding: str | None, card_type: str = "word") -> UUID:
        self.card_sequence += 1
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=self.card_sequence)
        with TestingSessionLocal() as db:
            card = Card(
                user_id=self.user_uuid,
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
                created_at=created_at,
                status="active",
            )
            db.add(card)
            db.commit()
            db.refresh(card)
            return card.id

    def create_four_cards(self) -> list[UUID]:
        return [
            self.create_card("abandon", "放弃"),
            self.create_card("borrow", "借入"),
            self.create_card("capture", "捕获"),
            self.create_card("deliver", "递送"),
        ]

    def ecdict_entries(self, *meanings: str) -> tuple[EcdictEntry, ...]:
        return tuple(
            EcdictEntry(
                word=f"dict{index}",
                translation=meaning,
                meanings=(meaning,),
                pos="n",
                frq=100 + index,
                bnc=200 + index,
            )
            for index, meaning in enumerate(meanings, start=1)
        )

    def test_v1_eligibility_and_exact_understanding_answer(self):
        ids = self.create_four_cards()
        self.create_card("empty-understanding", "")
        self.create_card("x" * 81, "过长内容")

        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 10, "restart": True},
        )
        self.assertEqual(200, response.status_code, response.text)
        data = response.json()
        self.assertEqual(4, data["progress"]["total"])
        returned_ids = {UUID(item["card_id"]) for item in data["items"]}
        self.assertEqual(set(ids), returned_ids)

        first = data["items"][0]
        self.assertEqual(4, len(first["options"]))
        option_texts = [option["text"] for option in first["options"]]
        self.assertIn(first["understanding"], option_texts)
        self.assertEqual(4, len(set(option_texts)))

    def test_ecdict_only_supplements_distractors(self):
        target_id = self.create_card("abandon", "完整的用户理解")
        self.create_card("borrow", "借入")

        fake_entries = (
            EcdictEntry(word="capture", translation="捕获", meanings=("捕获",), pos="v", frq=20, bnc=30),
            EcdictEntry(word="deliver", translation="递送", meanings=("递送",), pos="v", frq=25, bnc=35),
        )
        with patch("app.services.review_v1.get_dictionary_distractor_entries", return_value=fake_entries):
            response = self.client.post(
                "/api/review-sessions",
                headers=self.auth_headers(),
                json={"limit": 1, "restart": True},
            )
        self.assertEqual(200, response.status_code, response.text)
        item = response.json()["items"][0]
        self.assertEqual(str(target_id), item["card_id"])
        self.assertIn("完整的用户理解", [option["text"] for option in item["options"]])

        with TestingSessionLocal() as db:
            question = db.scalar(select(ReviewMcqQuestion).where(ReviewMcqQuestion.card_id == target_id))
            sources = {option["source"] for option in question.options_snapshot}
            self.assertIn("understanding", sources)
            self.assertIn("user_card", sources)
            self.assertIn("ecdict", sources)

    def test_understanding_cleared_before_display_excludes_card(self):
        ids = self.create_four_cards()
        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 4, "restart": True},
        )
        self.assertEqual(200, response.status_code, response.text)

        with TestingSessionLocal() as db:
            card = db.get(Card, ids[0])
            card.understanding = ""
            db.commit()

        with patch(
            "app.services.review_v1.get_dictionary_distractor_entries",
            return_value=self.ecdict_entries("词典干扰"),
        ):
            refreshed = self.client.get(
                "/api/reviews/today",
                headers=self.auth_headers(),
                params={"limit": 4},
            )
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        returned_ids = {UUID(item["card_id"]) for item in refreshed.json()["items"]}
        self.assertNotIn(ids[0], returned_ids)
        for item in refreshed.json()["items"]:
            self.assertNotIn("", [option["text"] for option in item["options"]])

    def test_stale_question_when_understanding_changes_after_client_receives_question(self):
        self.create_four_cards()
        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 4, "restart": True},
        )
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()["items"][0]

        with TestingSessionLocal() as db:
            card = db.get(Card, UUID(first["card_id"]))
            card.understanding = "新的理解"
            db.commit()

        submitted = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": response.json()["session_id"],
                "session_item_id": first["session_item_id"],
                "card_id": first["card_id"],
                "question_id": first["question_id"],
                "selected_option_id": first["options"][0]["option_id"],
            },
        )
        self.assertEqual(409, submitted.status_code)
        self.assertEqual("stale_question", submitted.json()["detail"])

    def test_wrong_repeat_once_and_updates_fsrs_again(self):
        self.create_four_cards()
        start_response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 4, "restart": True},
        )
        self.assertEqual(200, start_response.status_code, start_response.text)
        first = start_response.json()["items"][0]
        wrong_option = next(option for option in first["options"] if option["option_id"] != "correct")

        response = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": start_response.json()["session_id"],
                "session_item_id": first["session_item_id"],
                "card_id": first["card_id"],
                "question_id": first["question_id"],
                "selected_option_id": wrong_option["option_id"],
                "response_time_ms": 1234,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()["done"])
        self.assertEqual(5, response.json()["progress"]["total"])

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewAnswerLog).where(ReviewAnswerLog.card_id == UUID(first["card_id"])))
            self.assertIsNotNone(log)
            self.assertFalse(log.is_correct)
            self.assertEqual("Again", log.fsrs_rating)
            self.assertEqual(1234, log.response_time_ms)
            state = db.scalar(select(CardFsrsState).where(CardFsrsState.card_id == UUID(first["card_id"])))
            self.assertIsNotNone(state)
            repeat_items = list(
                db.scalars(
                    select(ReviewSessionItem).where(
                        ReviewSessionItem.session_id == UUID(start_response.json()["session_id"]),
                        ReviewSessionItem.card_id == UUID(first["card_id"]),
                        ReviewSessionItem.is_repeat.is_(True),
                    )
                )
            )
            self.assertEqual(1, len(repeat_items))

    def test_repeat_wrong_writes_second_log_without_third_repeat(self):
        target_id = self.create_card("abandon", "放弃")
        fake_entries = self.ecdict_entries("捕获", "递送", "借入")
        with patch("app.services.review_v1.get_dictionary_distractor_entries", return_value=fake_entries):
            start_response = self.client.post(
                "/api/review-sessions",
                headers=self.auth_headers(),
                json={"limit": 1, "restart": True},
            )
        self.assertEqual(200, start_response.status_code, start_response.text)
        first = start_response.json()["items"][0]
        wrong_option = next(option for option in first["options"] if option["option_id"] != "correct")

        first_wrong = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": start_response.json()["session_id"],
                "session_item_id": first["session_item_id"],
                "card_id": first["card_id"],
                "question_id": first["question_id"],
                "selected_option_id": wrong_option["option_id"],
            },
        )
        self.assertEqual(200, first_wrong.status_code, first_wrong.text)
        repeat = first_wrong.json()["next_item"]
        self.assertTrue(repeat["is_repeat"])
        self.assertEqual(2, repeat["attempt_no"])
        repeat_wrong_option = next(option for option in repeat["options"] if option["option_id"] != "correct")

        second_wrong = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": start_response.json()["session_id"],
                "session_item_id": repeat["session_item_id"],
                "card_id": repeat["card_id"],
                "question_id": repeat["question_id"],
                "selected_option_id": repeat_wrong_option["option_id"],
            },
        )
        self.assertEqual(200, second_wrong.status_code, second_wrong.text)
        self.assertTrue(second_wrong.json()["done"])

        with TestingSessionLocal() as db:
            logs = list(db.scalars(select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == target_id)))
            self.assertEqual(2, len(logs))
            self.assertEqual(["Again", "Again"], [log.fsrs_rating for log in logs])
            repeat_items = list(
                db.scalars(
                    select(ReviewSessionItem).where(
                        ReviewSessionItem.session_id == UUID(start_response.json()["session_id"]),
                        ReviewSessionItem.card_id == target_id,
                        ReviewSessionItem.is_repeat.is_(True),
                    )
                )
            )
            self.assertEqual(1, len(repeat_items))
            self.assertEqual(1, repeat_items[0].repeat_count)
            self.assertEqual(1, repeat_items[0].reappear_count)

    def test_ecdict_progressively_relaxes_until_three_distractors(self):
        target_id = self.create_card("improbabletarget", "目标理解")
        with TestingSessionLocal() as db:
            db.add(
                CardLexicalMetadata(
                    card_id=target_id,
                    content_normalized="improbabletarget",
                    edict_hit=True,
                    pos="v",
                    frq=1,
                    bnc=1,
                )
            )
            db.commit()

        calls = []

        def staged_ecdict(**kwargs):
            calls.append(kwargs)
            if kwargs["pos"] is None and kwargs["frq"] is None and kwargs["bnc"] is None:
                return self.ecdict_entries("宽松一", "宽松二", "宽松三")
            return ()

        with patch("app.services.review_v1.get_dictionary_distractor_entries", side_effect=staged_ecdict):
            response = self.client.post(
                "/api/review-sessions",
                headers=self.auth_headers(),
                json={"limit": 1, "restart": True},
            )
        self.assertEqual(200, response.status_code, response.text)
        item = response.json()["items"][0]
        self.assertEqual(str(target_id), item["card_id"])
        self.assertEqual(4, len(item["options"]))
        self.assertIn("目标理解", [option["text"] for option in item["options"]])
        self.assertGreaterEqual(len(calls), 4)
        self.assertTrue(any(call["pos"] is None and call["frq"] is None and call["bnc"] is None for call in calls))

    def test_card_edit_refreshes_unanswered_question_and_answer_log_is_immutable(self):
        ids = self.create_four_cards()
        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 4, "restart": True},
        )
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()["items"][0]

        with TestingSessionLocal() as db:
            card = db.get(Card, ids[0])
            card.content = "abandon ship"
            card.content_normalized = "abandon ship"
            card.understanding = "弃船"
            db.commit()

        refreshed = self.client.get(
            "/api/reviews/today",
            headers=self.auth_headers(),
            params={"limit": 4},
        )
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        refreshed_first = refreshed.json()["items"][0]
        self.assertEqual("abandon ship", refreshed_first["content"])
        self.assertEqual("弃船", refreshed_first["understanding"])
        self.assertIn("弃船", [option["text"] for option in refreshed_first["options"]])

        correct_option = next(option for option in refreshed_first["options"] if option["text"] == "弃船")
        submitted = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": refreshed.json()["session_id"],
                "session_item_id": refreshed_first["session_item_id"],
                "card_id": refreshed_first["card_id"],
                "question_id": refreshed_first["question_id"],
                "selected_option_id": correct_option["option_id"],
            },
        )
        self.assertEqual(200, submitted.status_code, submitted.text)

        with TestingSessionLocal() as db:
            card = db.get(Card, ids[0])
            card.content = "edited after answer"
            card.content_normalized = "edited after answer"
            card.understanding = "答题后修改"
            db.commit()

        with TestingSessionLocal() as db:
            log = db.scalar(select(ReviewAnswerLog).where(ReviewAnswerLog.card_id == ids[0]))
            self.assertEqual("abandon ship", log.prompt_content_snapshot)
            self.assertEqual("弃船", log.correct_answer_snapshot)

    def test_card_delete_keeps_answer_log_snapshot_complete(self):
        ids = self.create_four_cards()
        response = self.client.post(
            "/api/review-sessions",
            headers=self.auth_headers(),
            json={"limit": 4, "restart": True},
        )
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()["items"][0]
        correct_option = next(option for option in first["options"] if option["option_id"] == "correct")

        submitted = self.client.post(
            "/api/reviews/feedback",
            headers=self.auth_headers(),
            json={
                "client_action_id": f"answer-{uuid4()}",
                "session_id": response.json()["session_id"],
                "session_item_id": first["session_item_id"],
                "card_id": first["card_id"],
                "question_id": first["question_id"],
                "selected_option_id": correct_option["option_id"],
                "response_time_ms": 321,
            },
        )
        self.assertEqual(200, submitted.status_code, submitted.text)

        card_id = UUID(first["card_id"])
        with TestingSessionLocal() as db:
            before_delete = db.scalar(select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == card_id))
            self.assertIsNotNone(before_delete)
            before_snapshot = {
                "prompt": before_delete.prompt_content_snapshot,
                "correct": before_delete.correct_answer_snapshot,
                "options": list(before_delete.options_snapshot),
                "order": list(before_delete.option_order),
                "selected": before_delete.selected_answer_text,
                "is_correct": before_delete.is_correct,
                "before": dict(before_delete.fsrs_state_before_json),
                "after": dict(before_delete.fsrs_state_after_json),
            }

        deleted = self.client.delete(f"/api/cards/{card_id}", headers=self.auth_headers())
        self.assertEqual(200, deleted.status_code, deleted.text)

        with TestingSessionLocal() as db:
            after_delete = db.scalar(select(ReviewAnswerLog).where(ReviewAnswerLog.source_card_id == card_id))
            self.assertIsNotNone(after_delete)
            self.assertEqual(card_id, after_delete.source_card_id)
            self.assertEqual(before_snapshot["prompt"], after_delete.prompt_content_snapshot)
            self.assertEqual(before_snapshot["correct"], after_delete.correct_answer_snapshot)
            self.assertEqual(before_snapshot["options"], after_delete.options_snapshot)
            self.assertEqual(before_snapshot["order"], after_delete.option_order)
            self.assertEqual(before_snapshot["selected"], after_delete.selected_answer_text)
            self.assertEqual(before_snapshot["is_correct"], after_delete.is_correct)
            self.assertEqual(before_snapshot["before"], after_delete.fsrs_state_before_json)
            self.assertEqual(before_snapshot["after"], after_delete.fsrs_state_after_json)

    def test_lexical_metadata_create_update_and_understanding_update(self):
        with patch("app.services.lexical_metadata.get_dictionary_entry", return_value=None):
            created = self.client.post(
                "/api/cards",
                headers=self.auth_headers(),
                json={
                    "content": "unknownword",
                    "card_type": "word",
                    "understanding": "未知词",
                },
            )
        self.assertEqual(200, created.status_code, created.text)
        card_id = UUID(created.json()["id"])
        with TestingSessionLocal() as db:
            metadata = db.get(CardLexicalMetadata, card_id)
            self.assertIsNotNone(metadata)
            self.assertFalse(metadata.edict_hit)
            self.assertEqual("unknownword", metadata.content_normalized)
            first_updated_at = metadata.updated_at

        self.client.patch(
            f"/api/cards/{card_id}",
            headers=self.auth_headers(),
            json={"understanding": "只改理解", "base_version": created.json()["version"]},
        )
        with TestingSessionLocal() as db:
            metadata = db.get(CardLexicalMetadata, card_id)
            self.assertEqual(first_updated_at, metadata.updated_at)

        entry = EcdictEntry(word="borrow", translation="借入", meanings=("借入",), pos="v", frq=10, bnc=20)
        latest_version = self.client.get(f"/api/cards/{card_id}", headers=self.auth_headers()).json()["version"]
        with patch("app.services.lexical_metadata.get_dictionary_entry", return_value=entry):
            updated = self.client.patch(
                f"/api/cards/{card_id}",
                headers=self.auth_headers(),
                json={"content": "borrow", "base_version": latest_version},
            )
        self.assertEqual(200, updated.status_code, updated.text)
        with TestingSessionLocal() as db:
            metadata = db.get(CardLexicalMetadata, card_id)
            self.assertTrue(metadata.edict_hit)
            self.assertEqual("borrow", metadata.content_normalized)
            self.assertEqual("v", metadata.pos)
            self.assertEqual(10, metadata.frq)
            self.assertEqual(20, metadata.bnc)

    def test_backfill_lexical_metadata_is_repeatable(self):
        card_id = self.create_card("repeatable", "可重复")
        with TestingSessionLocal() as db:
            self.assertIsNone(db.get(CardLexicalMetadata, card_id))

        with (
            patch.object(backfill_card_lexical_metadata, "SessionLocal", TestingSessionLocal),
            patch("app.services.lexical_metadata.get_dictionary_entry", return_value=None),
            patch("sys.argv", ["backfill_card_lexical_metadata.py", "--batch-size", "10"]),
        ):
            self.assertEqual(0, backfill_card_lexical_metadata.main())
            self.assertEqual(0, backfill_card_lexical_metadata.main())

        with TestingSessionLocal() as db:
            rows = list(db.scalars(select(CardLexicalMetadata).where(CardLexicalMetadata.card_id == card_id)))
            self.assertEqual(1, len(rows))
            self.assertFalse(rows[0].edict_hit)

    def test_backfill_ignores_empty_normalized_content(self):
        card_id = self.create_card("", "空内容")
        with (
            patch.object(backfill_card_lexical_metadata, "SessionLocal", TestingSessionLocal),
            patch("app.services.lexical_metadata.get_dictionary_entry", return_value=None),
            patch("sys.argv", ["backfill_card_lexical_metadata.py", "--batch-size", "10"]),
        ):
            self.assertEqual(0, backfill_card_lexical_metadata.main())

        with TestingSessionLocal() as db:
            self.assertIsNone(db.get(CardLexicalMetadata, card_id))

    def test_backfill_ignores_overlong_content(self):
        card_id = self.create_card("x" * 81, "过长")
        with (
            patch.object(backfill_card_lexical_metadata, "SessionLocal", TestingSessionLocal),
            patch("app.services.lexical_metadata.get_dictionary_entry", return_value=None),
            patch("sys.argv", ["backfill_card_lexical_metadata.py", "--batch-size", "10"]),
        ):
            self.assertEqual(0, backfill_card_lexical_metadata.main())

        with TestingSessionLocal() as db:
            self.assertIsNone(db.get(CardLexicalMetadata, card_id))


if __name__ == "__main__":
    unittest.main()
