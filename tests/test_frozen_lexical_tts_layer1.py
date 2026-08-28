"""Layer 1 lexical evidence and phonetic/TTS boundary checks."""

from unittest.mock import patch

import pytest

from app.services.validator import validate_english


class FakeSuggestion:
    def __init__(self, term: str, distance: int):
        self.term = term
        self.distance = distance


class FakeSymSpell:
    def __init__(self, suggestions):
        self.suggestions = suggestions

    def lookup(self, *args, **kwargs):
        return self.suggestions


def _evidence(result, source):
    return next(item for item in result["evidence"] if item["source"] == source)


def test_LEX_ECDICT_001_exact_hit_is_positive_and_case_normalized():
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=object()
    ) as lookup:
        result = validate_english("  Because  ")

    lookup.assert_called_once_with("because")
    assert _evidence(result, "ecdict")["result"] == "hit"
    assert _evidence(result, "ecdict")["polarity"] == "positive"


@pytest.mark.parametrize(
    ("distance", "expected_result"),
    [(1, "suggestion"), (2, "no_suggestion")],
)
def test_LEX_SYMSPELL_001_distance_boundary_is_enforced(distance, expected_result):
    with (
        patch("app.services.validator.dictionary_available", return_value=True),
        patch("app.services.validator.get_dictionary_entry", return_value=None),
        patch("app.services.validator.Verbosity", type("FakeVerbosity", (), {"CLOSEST": object()})),
        patch(
            "app.services.validator._get_symspell",
            return_value=FakeSymSpell([FakeSuggestion("because", distance)]),
        ),
    ):
        result = validate_english("becuase")

    evidence = _evidence(result, "symspell")
    assert evidence["result"] == expected_result
    if expected_result == "suggestion":
        assert evidence["distance"] == distance


@pytest.mark.parametrize("text", ["ChatGPT", "GPT-5", "COVID-19", "Netflix"])
def test_LEX_FP_001_proper_noun_and_acronym_misses_are_not_hard_errors(text):
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=None
    ), patch("app.services.validator._get_symspell", return_value=FakeSymSpell([])):
        result = validate_english(text)
    assert result["level"] in {"pass", "warning"}
    assert result["errors"] == []


def test_LEX_002_unknown_word_without_lexical_evidence_is_not_positive_evidence():
    with patch("app.services.validator.dictionary_available", return_value=True), patch(
        "app.services.validator.get_dictionary_entry", return_value=None
    ), patch("app.services.validator._get_symspell", return_value=FakeSymSpell([])):
        result = validate_english("qzxvpl")
    assert _evidence(result, "ecdict")["result"] == "miss"
    assert _evidence(result, "ecdict")["polarity"] != "positive"


@pytest.mark.parametrize(
    ("text", "category"),
    [("because", "word"), ("give up", "phrase"), ("I love English.", "sentence")],
)
def test_TTS_002_phonetic_boundary_is_category_specific(text, category):
    result = validate_english(text)
    assert result["category"] == category
    if category in {"phrase", "sentence"}:
        assert category != "word"
