from __future__ import annotations

import importlib.metadata
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fsrs import Card as FsrsCard
from fsrs import Rating, Scheduler
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.card import Card, CardLexicalMetadata
from app.models.review import CardFsrsState, ReviewMcqQuestion
from app.models.user import User, utc_now
from app.services.ecdict_service import (
    dictionary_available,
    get_dictionary_distractor_entries,
    normalize_lexical_text,
)
from app.services.lexical_metadata import METADATA_VERSION, upsert_card_lexical_metadata


MAX_REVIEW_CONTENT_LENGTH = 80
MCQ_GENERATION_VERSION = "mcq-v1"
FSRS_SCHEDULER_NAME = "py-fsrs"
FSRS_SCHEDULER_VERSION = importlib.metadata.version("fsrs")


@dataclass(frozen=True)
class Distractor:
    text: str
    source: str
    card_id: UUID | None = None
    word: str | None = None
    pos: str | None = None
    frq: int | None = None
    bnc: int | None = None


@dataclass(frozen=True)
class FsrsReviewResult:
    rating: str
    review_log_json: dict
    state_before_json: dict
    state_after_json: dict
    scheduler_parameters: dict


class DistractorGenerationError(RuntimeError):
    pass


def _json_dict(value: str | dict) -> dict:
    if isinstance(value, dict):
        return value
    return json.loads(value)


def _normalize_answer(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _has_answer(card: Card) -> bool:
    return bool(card.understanding and card.understanding.strip())


def is_v1_review_eligible(card: Card) -> bool:
    return bool(
        card.deleted_at is None
        and card.status == "active"
        and card.content
        and len(card.content.strip()) <= MAX_REVIEW_CONTENT_LENGTH
        and _has_answer(card)
    )


def _scheduler() -> Scheduler:
    return Scheduler(learning_steps=(), relearning_steps=())


def scheduler_parameters_json() -> dict:
    return _json_dict(_scheduler().to_json())


def _fsrs_card_to_state(state: CardFsrsState, card: FsrsCard, now: datetime) -> None:
    card_json = _json_dict(card.to_json())
    state.fsrs_card_json = card_json
    state.due_at = card.due
    state.state = int(card.state)
    state.stability = card.stability
    state.difficulty = card.difficulty
    state.scheduler_name = FSRS_SCHEDULER_NAME
    state.scheduler_version = FSRS_SCHEDULER_VERSION
    state.scheduler_parameters = scheduler_parameters_json()
    state.last_reviewed_at = card.last_review
    state.updated_at = now


def get_or_create_fsrs_state(db: Session, user_id: UUID, card_id: UUID, now: datetime) -> CardFsrsState:
    state = db.scalar(select(CardFsrsState).where(CardFsrsState.card_id == card_id).with_for_update())
    if state is not None:
        return state

    fsrs_card = FsrsCard(due=now)
    state = CardFsrsState(
        user_id=user_id,
        card_id=card_id,
        due_at=fsrs_card.due,
        state=int(fsrs_card.state),
        stability=fsrs_card.stability,
        difficulty=fsrs_card.difficulty,
        fsrs_card_json=_json_dict(fsrs_card.to_json()),
        scheduler_name=FSRS_SCHEDULER_NAME,
        scheduler_version=FSRS_SCHEDULER_VERSION,
        scheduler_parameters=scheduler_parameters_json(),
    )
    db.add(state)
    db.flush()
    return state


def apply_fsrs_review(
    db: Session,
    *,
    user_id: UUID,
    card_id: UUID,
    is_correct: bool,
    reviewed_at: datetime,
) -> FsrsReviewResult:
    state = get_or_create_fsrs_state(db, user_id, card_id, reviewed_at)
    before_json = dict(state.fsrs_card_json or {})
    fsrs_card = FsrsCard.from_json(json.dumps(before_json))
    rating = Rating.Good if is_correct else Rating.Again
    scheduler = _scheduler()
    reviewed_card, review_log = scheduler.review_card(fsrs_card, rating, review_datetime=reviewed_at)
    after_json = _json_dict(reviewed_card.to_json())
    _fsrs_card_to_state(state, reviewed_card, reviewed_at)
    return FsrsReviewResult(
        rating="Good" if is_correct else "Again",
        review_log_json=_json_dict(review_log.to_json()),
        state_before_json=before_json,
        state_after_json=after_json,
        scheduler_parameters=_json_dict(scheduler.to_json()),
    )


def _metadata_for_card(db: Session, card: Card) -> CardLexicalMetadata | None:
    metadata = db.get(CardLexicalMetadata, card.id)
    if (
        metadata is None
        or metadata.content_normalized != card.content_normalized
        or metadata.metadata_version != METADATA_VERSION
    ):
        metadata = upsert_card_lexical_metadata(db, card)
        db.flush()
    return metadata


def _freq_value(metadata: CardLexicalMetadata | None) -> int | None:
    if metadata is None:
        return None
    return metadata.frq if metadata.frq is not None else metadata.bnc


def _freq_score(target: int | None, candidate: int | None) -> float:
    if target is None or candidate is None:
        return 0.0
    distance = abs(target - candidate)
    if distance <= 500:
        return 10.0
    if distance <= 2000:
        return 6.0
    if distance <= 8000:
        return 3.0
    return 0.0


def _freq_is_close(target: int | None, candidate: int | None) -> bool:
    if target is None or candidate is None:
        return False
    return abs(target - candidate) <= 2000


def _is_visible_answer_text(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) > 120:
        return False
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        return False
    if re.match(r"^https?://", text, flags=re.IGNORECASE):
        return False
    return True


def _is_valid_ecdict_meaning(value: str | None, *, word: str | None = None) -> bool:
    text = str(value or "").strip()
    if not _is_visible_answer_text(text):
        return False
    if word and _normalize_answer(text) == _normalize_answer(word):
        return False
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _candidate_cards(db: Session, user_id: UUID, target_id: UUID) -> list[tuple[Card, CardLexicalMetadata | None]]:
    return list(
        db.execute(
            select(Card, CardLexicalMetadata)
            .outerjoin(CardLexicalMetadata, CardLexicalMetadata.card_id == Card.id)
            .where(
                Card.user_id == user_id,
                Card.id != target_id,
                Card.deleted_at.is_(None),
                Card.status == "active",
                func.length(func.trim(Card.content)) <= MAX_REVIEW_CONTENT_LENGTH,
                func.length(func.trim(func.coalesce(Card.understanding, ""))) > 0,
            )
            .limit(500)
        ).all()
    )


def _option_dict(option_id: str, text: str, source: str, **extra) -> dict:
    return {
        "option_id": option_id,
        "text": text,
        "source": source,
        **{key: value for key, value in extra.items() if value is not None},
    }


def _add_distractor(chosen: list[Distractor], seen_answers: set[str], distractor: Distractor) -> bool:
    if not _is_visible_answer_text(distractor.text):
        return False
    key = _normalize_answer(distractor.text)
    if key in seen_answers:
        return False
    seen_answers.add(key)
    chosen.append(distractor)
    return True


def generate_distractors(db: Session, *, user: User, target_card: Card) -> list[Distractor]:
    target_answer_key = _normalize_answer(target_card.understanding)
    target_metadata = _metadata_for_card(db, target_card)
    target_freq = _freq_value(target_metadata)
    rows = _candidate_cards(db, user.id, target_card.id)
    seen_answers = {target_answer_key}
    chosen: list[Distractor] = []

    def same_pos(metadata: CardLexicalMetadata | None) -> bool:
        return bool(target_metadata and metadata and target_metadata.pos and metadata.pos == target_metadata.pos)

    def same_type(card: Card) -> bool:
        return card.card_type == target_card.card_type

    def close_freq(metadata: CardLexicalMetadata | None) -> bool:
        return _freq_is_close(target_freq, _freq_value(metadata))

    user_stages = (
        lambda card, metadata: same_type(card) and same_pos(metadata) and close_freq(metadata),
        lambda card, metadata: same_type(card) and same_pos(metadata),
        lambda card, metadata: same_type(card) and close_freq(metadata),
        lambda card, metadata: same_type(card),
        lambda card, metadata: same_pos(metadata),
        lambda card, metadata: True,
    )

    for stage in user_stages:
        stage_rows = [(card, metadata) for card, metadata in rows if stage(card, metadata)]
        stage_rows.sort(key=lambda row: (_freq_score(target_freq, _freq_value(row[1])), row[0].created_at), reverse=True)
        for card, metadata in stage_rows:
            answer = str(card.understanding or "").strip()
            distractor = Distractor(
                text=answer,
                source="user_card",
                card_id=card.id,
                pos=metadata.pos if metadata else None,
                frq=metadata.frq if metadata else None,
                bnc=metadata.bnc if metadata else None,
            )
            _add_distractor(chosen, seen_answers, distractor)
            if len(chosen) >= 3:
                return chosen

    excluded_words = {normalize_lexical_text(target_card.content)}
    ecdict_stages = (
        {
            "pos": target_metadata.pos if target_metadata else None,
            "frq": target_metadata.frq if target_metadata else None,
            "bnc": target_metadata.bnc if target_metadata else None,
        },
        {"pos": target_metadata.pos if target_metadata else None, "frq": None, "bnc": None},
        {
            "pos": None,
            "frq": target_metadata.frq if target_metadata else None,
            "bnc": target_metadata.bnc if target_metadata else None,
        },
        {"pos": None, "frq": None, "bnc": None},
    )

    for stage in ecdict_stages:
        entries = get_dictionary_distractor_entries(
            exclude_words=excluded_words,
            exclude_meanings=seen_answers,
            pos=stage["pos"],
            frq=stage["frq"],
            bnc=stage["bnc"],
            limit=120,
        )
        for entry in entries:
            text = entry.meanings[0] if entry.meanings else ""
            if not _is_valid_ecdict_meaning(text, word=entry.word):
                continue
            _add_distractor(
                chosen,
                seen_answers,
                Distractor(
                    text=text,
                    source="ecdict",
                    word=entry.word,
                    pos=entry.pos,
                    frq=entry.frq,
                    bnc=entry.bnc,
                ),
            )
            if len(chosen) >= 3:
                return chosen

    if not dictionary_available():
        raise DistractorGenerationError("ECDICT distractor resource is unavailable")
    raise DistractorGenerationError("Could not generate 3 legal distractors from user cards and ECDICT")


def can_generate_mcq(db: Session, *, user: User, target_card: Card) -> bool:
    if not is_v1_review_eligible(target_card):
        return False
    return len(generate_distractors(db, user=user, target_card=target_card)) >= 3


def build_options(correct_answer: str, distractors: list[Distractor]) -> tuple[list[dict], list[str]]:
    if len(distractors) < 3:
        raise ValueError("not_enough_distractors")
    options = [_option_dict("correct", correct_answer, "understanding")]
    for index, distractor in enumerate(distractors[:3], start=1):
        options.append(
            _option_dict(
                f"d{index}",
                distractor.text,
                distractor.source,
                card_id=str(distractor.card_id) if distractor.card_id else None,
                word=distractor.word,
                pos=distractor.pos,
                frq=distractor.frq,
                bnc=distractor.bnc,
            )
        )
    order = [option["option_id"] for option in options]
    random.shuffle(order)
    return options, order


def _question_is_current(question: ReviewMcqQuestion, card: Card) -> bool:
    return (
        question.answered_at is None
        and question.card_version == card.version
        and question.prompt_content == card.content
        and question.prompt_content_normalized == card.content_normalized
        and question.correct_answer == str(card.understanding or "").strip()
    )


def _parent_question_for_repeat(db: Session, session_id: UUID, card_id: UUID) -> ReviewMcqQuestion | None:
    return db.scalar(
        select(ReviewMcqQuestion)
        .where(
            ReviewMcqQuestion.session_id == session_id,
            ReviewMcqQuestion.card_id == card_id,
            ReviewMcqQuestion.is_repeat.is_(False),
            ReviewMcqQuestion.answered_at.is_not(None),
        )
        .order_by(ReviewMcqQuestion.created_at.desc())
        .limit(1)
    )


def ensure_question_for_item(
    db: Session,
    *,
    user: User,
    session_id: UUID,
    item_id: UUID,
    card: Card,
    is_repeat: bool,
) -> ReviewMcqQuestion:
    if not is_v1_review_eligible(card):
        raise ValueError("card_not_review_eligible")

    question = db.scalar(select(ReviewMcqQuestion).where(ReviewMcqQuestion.session_item_id == item_id))
    if question is not None and _question_is_current(question, card):
        return question

    parent = _parent_question_for_repeat(db, session_id, card.id) if is_repeat else None
    if parent and parent.prompt_content == card.content and parent.correct_answer == str(card.understanding or "").strip():
        options = list(parent.options_snapshot or [])
        option_order = [option["option_id"] for option in options]
        random.shuffle(option_order)
        parent_question_id = parent.id
    else:
        distractors = generate_distractors(db, user=user, target_card=card)
        options, option_order = build_options(str(card.understanding or "").strip(), distractors)
        parent_question_id = None

    if question is None:
        question = ReviewMcqQuestion(
            user_id=user.id,
            session_id=session_id,
            session_item_id=item_id,
            card_id=card.id,
        )
        db.add(question)

    question.parent_question_id = parent_question_id
    question.attempt_no = 2 if is_repeat else 1
    question.is_repeat = is_repeat
    question.card_version = card.version
    question.prompt_content = card.content
    question.prompt_content_normalized = card.content_normalized
    question.correct_answer = str(card.understanding or "").strip()
    question.correct_answer_source = "understanding"
    question.options_snapshot = options
    question.option_order = option_order
    question.correct_option_id = "correct"
    question.generation_version = MCQ_GENERATION_VERSION
    question.updated_at = utc_now()
    db.flush()
    return question


def question_response_options(question: ReviewMcqQuestion) -> list[dict]:
    by_id = {option["option_id"]: option for option in list(question.options_snapshot or [])}
    return [
        {"option_id": option_id, "text": by_id[option_id]["text"]}
        for option_id in list(question.option_order or [])
        if option_id in by_id
    ]


def question_selected_option(question: ReviewMcqQuestion, selected_option_id: str) -> dict | None:
    for option in list(question.options_snapshot or []):
        if option.get("option_id") == selected_option_id:
            return option
    return None


def select_review_v1_cards(db: Session, *, user_id: UUID, limit: int, now: datetime) -> list[Card]:
    return list(
        db.scalars(
            select(Card)
            .outerjoin(CardFsrsState, CardFsrsState.card_id == Card.id)
            .where(
                Card.user_id == user_id,
                Card.deleted_at.is_(None),
                Card.status == "active",
                func.length(func.trim(Card.content)) <= MAX_REVIEW_CONTENT_LENGTH,
                func.length(func.trim(func.coalesce(Card.understanding, ""))) > 0,
                or_(CardFsrsState.card_id.is_(None), CardFsrsState.due_at <= now),
            )
            .order_by(CardFsrsState.due_at.is_(None).desc(), CardFsrsState.due_at, Card.created_at, Card.id)
            .limit(limit)
        )
    )
