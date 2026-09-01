from uuid import uuid4
from unittest.mock import patch
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models.user import User
from app.schemas.card import CardCreate, CardUpdate
from app.services.card_service import apply_card_update, create_card
from app.services.validation_decision import ValidationDecisionInput, decide_validation
from app.services.validator import validate_english


engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeSuggestion:
    def __init__(self, term, distance):
        self.term = term
        self.distance = distance


class FakeSymSpell:
    def __init__(self, suggestions):
        self.suggestions = suggestions

    def lookup(self, *args, **kwargs):
        return self.suggestions


def evidence_by_source(result, source):
    return next(item for item in result["evidence"] if item["source"] == source)


@pytest.fixture
def db_session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_create_card_rejects_hard_rule_invalid_content(db_session):
    user_id = uuid4()
    db_session.add(User(id=user_id, wx_openid=f"openid-{user_id}"))
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        create_card(
            db_session,
            CardCreate(
                user_id=user_id,
                content="https://example.com",
                card_type="word",
                local_temp_id="hard-rule-url",
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "invalid_english_content"


@pytest.mark.parametrize(
    "text",
    [
        "gonna",
        "ain't",
        "Netflix",
        "ChatGPT",
        "LOL",
        "U.S.",
        "e.g.",
        "can't",
        "mother-in-law",
        "GPT-5",
        "COVID-19",
    ],
)
def test_category_detection_v1_detects_words(text):
    assert validate_english(text)["category"] == "word"


def test_ecdict_word_hit_is_positive_evidence():
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=object()
    ):
        result = validate_english("purse")

    assert result["level"] != "error"
    assert result["evidence"][0]["result"] == "hit"
    assert result["evidence"][0]["polarity"] == "positive"
    assert result["level"] == "pass"


@pytest.mark.parametrize("text", ["ChatGPT", "Netflix", "Kyrie", "rizz", "gonna", "ain't"])
def test_ecdict_miss_is_neutral_for_unlisted_words(text):
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=None
    ):
        result = validate_english(text)

    assert result["level"] != "error"
    assert result["evidence"][0]["result"] == "miss"
    assert result["evidence"][0]["polarity"] == "neutral"


@pytest.mark.parametrize("text", ["ChatGPT", "Kyrie"])
def test_ecdict_miss_without_warning_is_pass(text):
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=None
    ):
        result = validate_english(text)

    assert result["level"] == "pass"
    assert result["warnings"] == []
    assert evidence_by_source(result, "ecdict")["polarity"] == "neutral"


def test_ecdict_phrase_lookup_is_neutral_on_miss():
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=None
    ) as lookup:
        result = validate_english("no way")

    lookup.assert_called_once_with("no way")
    assert result["level"] != "error"
    assert result["evidence"][0]["result"] == "miss"
    assert result["evidence"][0]["polarity"] == "neutral"


@pytest.mark.parametrize("text", ["This is a normal sentence.", "This is the first sentence. This is the second sentence."])
def test_ecdict_is_skipped_for_sentences_and_paragraphs(text):
    with patch("app.services.validator.dictionary_available") as available, patch(
        "app.services.validator.get_dictionary_entry"
    ) as lookup:
        result = validate_english(text)

    available.assert_not_called()
    lookup.assert_not_called()
    assert result["evidence"][0]["result"] == "skipped"
    assert result["evidence"][0]["polarity"] == "neutral"


def test_ecdict_failure_is_fail_open():
    with patch("app.services.validator.dictionary_available", side_effect=RuntimeError("db unavailable")):
        result = validate_english("purse")

    assert result["level"] in {"pass", "warning"}
    assert result["evidence"][0]["result"] == "unavailable"
    assert result["evidence"][0]["polarity"] == "neutral"


@pytest.mark.parametrize(("text", "suggestion"), [("recieve", "receive"), ("helo", "hello")])
def test_symspell_suggestion_is_warning_evidence(text, suggestion):
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=None),
        patch("app.services.validator.Verbosity", type("FakeVerbosity", (), {"CLOSEST": object()})),
        patch(
            "app.services.validator._get_symspell",
            return_value=FakeSymSpell([FakeSuggestion(suggestion, 1)]),
        ),
    ):
        result = validate_english(text)

    symspell = evidence_by_source(result, "symspell")
    assert result["level"] == "warning"
    assert result["errors"] == []
    assert symspell["result"] == "suggestion"
    assert symspell["polarity"] == "warning"
    assert symspell["suggestion"] == suggestion
    assert symspell["distance"] == 1
    assert any(suggestion in warning for warning in result["warnings"])


def test_symspell_distance_two_is_not_promoted_to_suggestion():
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=None),
        patch("app.services.validator.Verbosity", type("FakeVerbosity", (), {"CLOSEST": object()})),
        patch(
            "app.services.validator._get_symspell",
            return_value=FakeSymSpell([FakeSuggestion("because", 2)]),
        ),
    ):
        result = validate_english("becasee")

    symspell = evidence_by_source(result, "symspell")
    assert symspell["result"] == "no_suggestion"
    assert "suggestion" not in symspell
    assert "distance" not in symspell
    assert result["warningTypes"] == []
    assert result["canAnalyze"] is True
    assert result["canPronounce"] is False


def test_decision_engine_hard_rule_precedence_over_warning_and_positive_evidence():
    result = decide_validation(
        ValidationDecisionInput(
            hard_rule_errors=["英文内容请只填写英文，不能包含中文字符。"],
            warnings=["Harper grammar: Review this wording."],
            evidence=[
                {
                    "source": "ecdict",
                    "type": "lexical_match",
                    "result": "hit",
                    "polarity": "positive",
                },
                {
                    "source": "harper",
                    "type": "grammar",
                    "result": "lint",
                    "polarity": "warning",
                },
            ],
            detected_category="unknown",
            requested_category="word",
            normalized_text="hello 中文",
        )
    )

    assert result["level"] == "error"
    assert result["warnings"] == []
    assert result["errors"]


def test_decision_engine_positive_evidence_does_not_override_warning():
    result = decide_validation(
        ValidationDecisionInput(
            hard_rule_errors=[],
            warnings=["这段内容看起来更像单词。"],
            evidence=[
                {
                    "source": "ecdict",
                    "type": "lexical_match",
                    "result": "hit",
                    "polarity": "positive",
                }
            ],
            detected_category="word",
            requested_category="phrase",
            normalized_text="purse",
        )
    )

    assert result["level"] == "warning"


def test_decision_engine_dedupes_identical_warning_messages():
    result = decide_validation(
        ValidationDecisionInput(
            hard_rule_errors=[],
            warnings=[
                "Spelling: recieve -> receive",
                "Spelling: recieve -> receive",
            ],
            evidence=[
                {"source": "symspell", "polarity": "warning"},
                {"source": "harper", "polarity": "warning"},
            ],
            detected_category="word",
            requested_category=None,
            normalized_text="recieve",
        )
    )

    assert result["level"] == "warning"
    assert result["warnings"] == ["Spelling: recieve -> receive"]


def test_ecdict_hit_skips_symspell_warning():
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=object()),
        patch("app.services.validator._get_symspell") as symspell,
    ):
        result = validate_english("purse")

    symspell.assert_not_called()
    assert result["level"] != "error"
    assert not any("拼写可能有误" in warning for warning in result["warnings"])
    assert evidence_by_source(result, "ecdict")["result"] == "hit"
    assert evidence_by_source(result, "symspell")["result"] == "skipped"


@pytest.mark.parametrize("text", ["ChatGPT", "Netflix", "Kyrie", "rizz", "bruh", "gonna", "ain't"])
def test_symspell_unknown_is_neutral_for_unlisted_words(text):
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=None),
        patch("app.services.validator.Verbosity", type("FakeVerbosity", (), {"CLOSEST": object()})),
        patch("app.services.validator._get_symspell", return_value=FakeSymSpell([])),
    ):
        result = validate_english(text)

    assert result["level"] != "error"
    assert not any("拼写可能有误" in warning for warning in result["warnings"])
    assert evidence_by_source(result, "symspell")["polarity"] == "neutral"


@pytest.mark.parametrize("text", ["no way", "This is a normal sentence."])
def test_symspell_skips_non_word_categories(text):
    with patch("app.services.validator._get_symspell") as symspell:
        result = validate_english(text)

    symspell.assert_not_called()
    assert evidence_by_source(result, "symspell")["result"] == "skipped"
    assert evidence_by_source(result, "symspell")["polarity"] == "neutral"


def test_symspell_failure_is_fail_open():
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=None),
        patch("app.services.validator._get_symspell", side_effect=RuntimeError("symspell unavailable")),
    ):
        result = validate_english("recieve")

    assert result["level"] in {"pass", "warning"}
    assert result["errors"] == []
    assert evidence_by_source(result, "symspell")["result"] == "unavailable"
    assert evidence_by_source(result, "symspell")["polarity"] == "neutral"


def test_hard_rule_invalid_skips_ecdict_lookup():
    with (
        patch("app.services.validator.dictionary_available") as available,
        patch("app.services.validator.get_dictionary_entry") as lookup,
        patch("app.services.validator._get_symspell") as symspell,
    ):
        result = validate_english("hello 中文")

    assert result["level"] == "error"
    assert result["category"] == "unknown"
    available.assert_not_called()
    lookup.assert_not_called()
    symspell.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "hello\\world",
        "hello\u0000world",
        "hello\u001fworld",
        "hello\u007fworld",
        "hello\u200bworld",
        "hello\u200cworld",
        "hello\u200dworld",
        "\ufeffhello",
        "hello\u202aworld",
        "hello\u202eworld",
        "hello\nworld",
        "hello\tworld",
    ],
)
def test_raw_forbidden_characters_are_hard_rule_and_skip_downstream(text):
    with (
        patch("app.services.validator.dictionary_available") as available,
        patch("app.services.validator.get_dictionary_entry") as lookup,
        patch("app.services.validator._get_symspell") as symspell,
        patch("app.services.validator.get_harper_evidence") as harper,
    ):
        result = validate_english(text, requested_category="sentence")

    assert result["level"] == "error"
    assert result["category"] == "unknown"
    assert result["warnings"] == []
    assert result["warningTypes"] == []
    assert result["canSave"] is False
    assert result["canAnalyze"] is False
    assert result["canPronounce"] is False
    available.assert_not_called()
    lookup.assert_not_called()
    symspell.assert_not_called()
    harper.assert_not_called()


@pytest.mark.parametrize("text", ["hello world", "Really?!", "can't stop"])
def test_normal_text_without_forbidden_characters_is_not_hard_rule(text):
    result = validate_english(text)

    assert result["level"] != "error"
    assert result["errors"] == []


def test_hard_rule_error_is_invalid_for_mixed_chinese_and_english():
    result = validate_english("hello 中文")

    assert result["level"] == "error"
    assert result["warnings"] == []
    assert result["errors"]


@pytest.mark.parametrize("text", ["no way", "what the hell", "on my way", "soooo good"])
def test_category_detection_v1_detects_phrases(text):
    assert validate_english(text)["category"] == "phrase"


@pytest.mark.parametrize(
    "text",
    ["Coming?", "So good.", "I dunno.", "What happened?", "I really like this movie."],
)
def test_category_detection_v1_detects_sentences(text):
    assert validate_english(text)["category"] == "sentence"


def test_category_detection_v1_detects_paragraphs_without_hard_rejecting_long_text():
    short_paragraph = "This is the first sentence. This is the second sentence."
    short_result = validate_english(short_paragraph)
    assert short_result["category"] == "paragraph"
    assert short_result["level"] != "error"

    long_paragraph = (
        "This is a normal English sentence with useful context for review. "
        "This is another normal English sentence that keeps the same paragraph readable. "
        "The learner may want to save this longer material because it came from a real article. "
        "The backend should classify the content as a paragraph without rejecting it. "
        "These sentences are intentionally repeated to keep the text above five hundred characters. "
        "This is a normal English sentence with useful context for review. "
        "This is another normal English sentence that keeps the same paragraph readable. "
        "The learner may want to save this longer material because it came from a real article."
    )
    assert len(long_paragraph) > 500
    long_result = validate_english(long_paragraph)
    assert long_result["category"] == "paragraph"
    assert long_result["level"] != "error"


@pytest.mark.parametrize(
    ("text", "requested_category", "detected_category", "expected_label"),
    [
        ("gonna", "phrase", "word", "单词"),
        ("no way", "word", "phrase", "短语"),
        ("So good.", "phrase", "sentence", "句子"),
    ],
)
def test_category_mismatch_returns_warning_not_error(
    text,
    requested_category,
    detected_category,
    expected_label,
):
    result = validate_english(text, requested_category=requested_category)

    assert result["level"] == "warning"
    assert result["category"] == detected_category
    assert result["errors"] == []
    assert any(expected_label in warning for warning in result["warnings"])


def test_category_auto_does_not_return_mismatch_warning():
    result = validate_english("gonna", requested_category="auto")

    assert result["category"] == "word"
    assert not any("看起来更像" in warning for warning in result["warnings"])


def test_category_detection_does_not_change_hard_rule_rejection():
    result = validate_english("hello 中文", requested_category="word")

    assert result["level"] == "error"
    assert result["category"] == "unknown"
    assert result["warnings"] == []


def test_create_card_allows_long_normal_english_content(db_session):
    user_id = uuid4()
    db_session.add(User(id=user_id, wx_openid=f"openid-{user_id}"))
    db_session.commit()
    long_text = "This is a normal English sentence with useful context. " * 12
    assert len(long_text) > 500

    card = create_card(
        db_session,
        CardCreate(
            user_id=user_id,
            content=long_text,
            card_type="sentence",
            local_temp_id="long-normal-english",
        ),
    )

    assert card.content == long_text.strip()


def test_create_card_allows_category_mismatch_with_warning(db_session):
    user_id = uuid4()
    db_session.add(User(id=user_id, wx_openid=f"openid-{user_id}"))
    db_session.commit()

    card = create_card(
        db_session,
        CardCreate(
            user_id=user_id,
            content="gonna",
            card_type="phrase",
            local_temp_id="category-mismatch-word-as-phrase",
        ),
    )

    assert card.content == "gonna"
    assert card.card_type == "phrase"
    assert card.analysis_level == "warning"
    assert any("单词" in message for message in card.analysis_messages)


def test_analyze_text_allows_category_mismatch_with_warning():
    from app.services.analyzer import analyze_text

    with (
        patch("app.services.analyzer.get_cache", return_value=None),
        patch("app.services.analyzer.set_cache"),
        patch(
            "app.services.analyzer.generate_analysis_with_ollama",
            return_value={
                "meaning": "将要",
                "exampleSentence": "I'm gonna leave soon.",
                "exampleTranslation": "我马上要离开。",
                "synonyms": [],
                "similarPhrases": [],
            },
        ),
        patch("app.services.analyzer.generate_understanding", return_value="将要；非正式表达"),
    ):
        result = analyze_text("gonna", card_type="phrase")

    assert result["ok"] is True
    assert result["level"] == "warning"
    assert result["category"] == "word"
    assert result["errors"] == []
    assert any("单词" in warning for warning in result["warnings"])


def test_create_card_normalizes_content_on_backend(db_session):
    user_id = uuid4()
    db_session.add(User(id=user_id, wx_openid=f"openid-{user_id}"))
    db_session.commit()

    card = create_card(
        db_session,
        CardCreate(
            user_id=user_id,
            content="  So good\u3002  ",
            card_type="sentence",
            local_temp_id="normalize-create",
        ),
    )

    assert card.content == "So good."
    assert card.content_normalized == "so good."


def test_update_card_normalizes_and_revalidates_content(db_session):
    user_id = uuid4()
    db_session.add(User(id=user_id, wx_openid=f"openid-{user_id}"))
    db_session.commit()
    card = create_card(
        db_session,
        CardCreate(
            user_id=user_id,
            content="So good.",
            card_type="sentence",
            local_temp_id="normalize-update",
        ),
    )

    apply_card_update(card, CardUpdate(content="  I don \u2019 t know \u3002  "))
    assert card.content == "I don't know."
    assert card.content_normalized == "i don't know."

    with pytest.raises(HTTPException) as exc_info:
        apply_card_update(card, CardUpdate(content="hello \u4e2d\u6587"))

    assert exc_info.value.status_code == 422
    assert card.content == "I don't know."


class FakeHarperResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def harper_settings(enabled=True, timeout=0.5):
    return SimpleNamespace(
        harper_enabled=enabled,
        harper_base_url="http://127.0.0.1:8082",
        harper_timeout_seconds=timeout,
    )


def test_harper_grammar_match_is_warning_evidence():
    response = FakeHarperResponse(
        {
            "lints": [
                {
                    "message": "The verb form may be incorrect.",
                    "offset": 2,
                    "length": 4,
                    "replacements": ["went"],
                    "kind": "Grammar",
                }
            ]
        }
    )
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response) as post,
    ):
        result = validate_english("I goed there yesterday.")

    harper = evidence_by_source(result, "harper")
    assert result["level"] == "warning"
    assert result["errors"] == []
    assert harper["type"] == "grammar"
    assert harper["polarity"] == "warning"
    assert harper["replacements"] == ["went"]
    post.assert_called_once()


def test_harper_usage_is_content_warning():
    response = FakeHarperResponse(
        {
            "lints": [
                {
                    "message": "Review this wording.",
                    "offset": 0,
                    "length": 2,
                    "replacements": [],
                    "kind": "style",
                }
            ]
        }
    )
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("So good.")

    harper = evidence_by_source(result, "harper")
    assert result["level"] == "warning"
    assert result["errors"] == []
    assert result["warningTypes"] == ["CONTENT_WARNING"]
    assert result["canSave"] is True
    assert result["canAnalyze"] is False
    assert result["canPronounce"] is False
    assert harper["type"] == "usage"
    assert harper["polarity"] == "warning"


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        httpx.ConnectError("connection refused"),
        RuntimeError("server error"),
    ],
)
def test_harper_failure_is_fail_open(failure):
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", side_effect=failure),
    ):
        result = validate_english("I really like this movie.")

    harper = evidence_by_source(result, "harper")
    assert result["level"] in {"pass", "warning"}
    assert result["errors"] == []
    assert harper["result"] == "unavailable"
    assert harper["polarity"] == "neutral"


def test_harper_unavailable_becomes_system_warning_and_fails_open():
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", side_effect=httpx.ConnectError("connection refused")),
    ):
        result = validate_english("I really like this movie.")

    assert result["level"] == "warning"
    assert result["warningTypes"] == ["SYSTEM_WARNING"]
    assert result["warnings"]
    assert result["errors"] == []
    assert evidence_by_source(result, "harper")["result"] == "unavailable"


def test_harper_http_500_is_fail_open():
    response = FakeHarperResponse({})

    def raise_http_error():
        raise httpx.HTTPStatusError("500", request=None, response=None)

    response.raise_for_status = raise_http_error
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("I really like this movie.")

    assert evidence_by_source(result, "harper")["result"] == "unavailable"
    assert result["errors"] == []


def test_harper_disabled_is_skipped_without_request():
    with (
        patch("app.services.harper_service.settings", harper_settings(False)),
        patch("app.services.harper_service.httpx.post") as post,
    ):
        result = validate_english("I really like this movie.")

    assert evidence_by_source(result, "harper")["result"] == "skipped"
    assert evidence_by_source(result, "harper")["polarity"] == "neutral"
    post.assert_not_called()


def test_hard_rule_invalid_skips_harper():
    with patch("app.services.validator.get_harper_evidence") as provider:
        result = validate_english("hello 中文")

    assert result["level"] == "error"
    provider.assert_not_called()


def test_word_skips_harper():
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post") as post,
    ):
        result = validate_english("purse")

    assert evidence_by_source(result, "harper")["result"] == "skipped"
    post.assert_not_called()


def test_harper_no_lint_does_not_create_warning():
    response = FakeHarperResponse({"lints": []})
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("I really like this movie.")

    assert result["level"] == "pass"
    assert result["warnings"] == []
    assert evidence_by_source(result, "harper")["result"] == "no_lint"


def test_multiple_warnings_keep_category_mismatch_and_harper_warning():
    response = FakeHarperResponse(
        {
            "lints": [
                {
                    "message": "Review this wording.",
                    "offset": 0,
                    "length": 2,
                    "replacements": [],
                    "kind": "Grammar",
                }
            ]
        }
    )
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("So good.", requested_category="phrase")

    assert result["level"] == "warning"
    assert result["errors"] == []
    assert any("Harper grammar" in warning for warning in result["warnings"])
    assert any("句子" in warning for warning in result["warnings"])


def test_control_character_is_hard_rule_and_blocks_all_capabilities():
    result = validate_english("hello\u0000world", requested_category="sentence")

    assert result["level"] == "error"
    assert result["category"] == "unknown"
    assert result["warnings"] == []
    assert result["errors"]
    assert result["canSave"] is False
    assert result["canAnalyze"] is False
    assert result["canPronounce"] is False


def test_category_mismatch_is_advisory_and_does_not_block_ai_or_tts():
    result = validate_english("gonna", requested_category="phrase")

    assert result["level"] == "warning"
    assert result["warningTypes"] == ["ADVISORY_WARNING"]
    assert result["canSave"] is True
    assert result["canAnalyze"] is True
    assert result["canPronounce"] is True


def test_local_punctuation_anomaly_is_not_warning():
    result = validate_english("Really?!")

    assert result["level"] == "pass"
    assert result["warnings"] == []
    assert result["warningTypes"] == []
    assert result["canAnalyze"] is True
    assert result["canPronounce"] is True


def test_harper_usage_warning_blocks_ai_and_tts_but_allows_save():
    response = FakeHarperResponse(
        {
            "lints": [
                {
                    "message": "Review this wording.",
                    "offset": 0,
                    "length": 2,
                    "replacements": [],
                    "kind": "style",
                }
            ]
        }
    )
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("So good.")

    harper = evidence_by_source(result, "harper")
    assert result["level"] == "warning"
    assert result["errors"] == []
    assert result["warningTypes"] == ["CONTENT_WARNING"]
    assert result["canSave"] is True
    assert result["canAnalyze"] is False
    assert result["canPronounce"] is False
    assert harper["type"] == "usage"
    assert harper["polarity"] == "warning"


def test_local_and_harper_punctuation_do_not_create_warning():
    response = FakeHarperResponse(
        {
            "lints": [
                {
                    "message": "Review this punctuation.",
                    "offset": 0,
                    "length": 2,
                    "replacements": [],
                    "kind": "typographical",
                }
            ]
        }
    )
    with (
        patch("app.services.harper_service.settings", harper_settings()),
        patch("app.services.harper_service.httpx.post", return_value=response),
    ):
        result = validate_english("Really?!")

    harper = evidence_by_source(result, "harper")
    assert result["level"] == "pass"
    assert result["warnings"] == []
    assert result["warningTypes"] == []
    assert result["canAnalyze"] is True
    assert result["canPronounce"] is True
    assert harper["type"] == "punctuation"
    assert harper["polarity"] == "warning"


def test_analyze_text_force_refresh_bypasses_cache_and_calls_model():
    from app.services.analyzer import analyze_text

    cached = {
        "ok": True,
        "level": "pass",
        "category": "word",
        "normalizedText": "gonna",
        "translation": "cached",
        "exampleSentence": "I'm gonna go now.",
        "warnings": [],
    }
    fresh = {
        "meaning": "将要",
        "exampleSentence": "I'm gonna leave soon.",
        "exampleTranslation": "我马上要离开。",
        "synonyms": [],
        "similarPhrases": [],
    }

    with (
        patch("app.services.analyzer.get_cache", return_value=cached) as get_cache_mock,
        patch("app.services.analyzer.set_cache"),
        patch("app.services.analyzer.generate_analysis_with_ollama", return_value=fresh) as model,
        patch("app.services.analyzer.generate_understanding", return_value="将要；非正式表达"),
    ):
        result = analyze_text("gonna", card_type="word", force_refresh=True)

    get_cache_mock.assert_not_called()
    model.assert_called_once()
    assert result["ok"] is True
    assert result["cacheHit"] is False
    assert result["translation"] == "将要"
    assert result["exampleSentence"] == "I'm gonna leave soon."


def test_analyze_text_without_force_refresh_can_use_cache():
    from app.services.analyzer import analyze_text

    cached = {
        "ok": True,
        "level": "pass",
        "category": "word",
        "normalizedText": "gonna",
        "translation": "cached",
        "exampleSentence": "I'm gonna go now.",
        "warnings": [],
    }

    with (
        patch("app.services.analyzer.get_cache", return_value=cached) as get_cache_mock,
        patch("app.services.analyzer.generate_analysis_with_ollama") as model,
    ):
        result = analyze_text("gonna", card_type="word")

    get_cache_mock.assert_called_once()
    model.assert_not_called()
    assert result["cacheHit"] is True
    assert result["translation"] == "cached"


def test_ollama_prompt_uses_sentence_not_phrase_for_short_sentence():
    from app.services.ollama_example import _build_payload

    payload = _build_payload(
        "I really like this movie.",
        None,
        category="sentence",
        strict_retry=False,
        format_spec={"type": "object"},
    )

    assert "Analyze this English sentence" in payload["prompt"]
    assert "Do not treat the whole sentence as a phrase" in payload["prompt"]
    assert "Analyze this English phrase" not in payload["prompt"]


def test_ollama_prompt_uses_translation_only_for_long_sentence_and_paragraph():
    from app.services.ollama_example import _build_payload, _translation_only_json_schema_format

    long_sentence = (
        "This is a longer sentence that should only be translated because it has enough "
        "characters to exceed the short sentence analysis boundary for learners."
    )
    sentence_payload = _build_payload(
        long_sentence,
        None,
        category="sentence",
        strict_retry=False,
        format_spec=_translation_only_json_schema_format(),
    )
    paragraph_payload = _build_payload(
        "This is the first sentence. This is the second sentence.",
        None,
        category="paragraph",
        strict_retry=False,
        format_spec=_translation_only_json_schema_format(),
    )

    assert "Translate this English sentence" in sentence_payload["prompt"]
    assert "exampleSentence" not in sentence_payload["prompt"]
    assert sentence_payload["format"]["required"] == ["meaning"]
    assert "Translate this English paragraph" in paragraph_payload["prompt"]
    assert "exampleSentence" not in paragraph_payload["prompt"]
    assert paragraph_payload["format"]["required"] == ["meaning"]


def test_analyze_text_long_sentence_only_returns_translation_fields():
    from app.services.analyzer import analyze_text

    long_sentence = (
        "This is a longer sentence that should only be translated because it has enough "
        "characters to exceed the short sentence analysis boundary for learners."
    )

    with (
        patch("app.services.analyzer.get_cache", return_value=None),
        patch("app.services.analyzer.set_cache"),
        patch(
            "app.services.analyzer.generate_analysis_with_ollama",
            return_value={"meaning": "这是一个较长的句子，只需要翻译。"},
        ) as model,
        patch("app.services.analyzer.generate_understanding", return_value="这是一个较长的句子，只需要翻译。"),
    ):
        result = analyze_text(long_sentence, card_type="sentence")

    model.assert_called_once()
    assert result["ok"] is True
    assert result["category"] == "sentence"
    assert result["translation"] == "这是一个较长的句子，只需要翻译。"
    assert result["exampleSentence"] is None
    assert result["dialogue"] == {"english": [], "chinese": []}
