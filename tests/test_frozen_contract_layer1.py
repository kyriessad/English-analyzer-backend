"""Executable checks for the frozen Product Contract.

These tests deliberately assert the contract vocabulary, rather than adapting
to the current implementation. Missing fields or old semantics are expected
to fail and are reported as implementation gaps.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.validation_decision import ValidationDecisionInput, decide_validation
from app.services.validator import normalize_text, validate_english


FIXTURE_DIR = Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIXTURE_DIR / "sample_corpus.json").read_text(encoding="utf-8"))
RULES = json.loads((FIXTURE_DIR / "rule_ids.json").read_text(encoding="utf-8"))


def _samples_by_id():
    return {item["sample_id"]: item for item in CORPUS["english_samples"]}


def _decision_for_contract_case(case):
    """Call the Decision Engine with the frozen input contract.

    ``warning_types`` is intentionally passed as a keyword. If the production
    engine has not adopted typed warnings yet, this raises TypeError and keeps
    the implementation gap visible instead of weakening the oracle.
    """
    warning_types = case.get("input_warning_types", case["expected_warning_types"])
    expected_evidence = case.get("expected_evidence", {})
    evidence = []
    if expected_evidence.get("ecdict") == "hit":
        evidence.append({"source": "ecdict", "result": "hit", "polarity": "positive"})
    if expected_evidence.get("symspell") == "exact":
        evidence.append({"source": "symspell", "result": "exact", "polarity": "neutral"})

    return decide_validation(
        ValidationDecisionInput(
            hard_rule_errors=["contract hard error"] if case["expected_level"] == "error" else [],
            warnings=[f"{value}::{code}" for value, code in zip(warning_types, case["expected_codes"])],
            evidence=evidence,
            detected_category=_samples_by_id()[case["sample_id"]]["expected_category"],
            requested_category=case["requested_category"],
            normalized_text=_samples_by_id()[case["sample_id"]]["normalized_text"],
            warning_types=warning_types,
        )
    )


def test_rule_registry_has_stable_unique_ids():
    ids = [item["rule_id"] for item in RULES["rules"]]
    assert ids
    assert len(ids) == len(set(ids))
    assert all(item["layers"] for item in RULES["rules"])


def test_corpus_ids_are_unique_and_scenarios_reference_existing_samples():
    samples = CORPUS["english_samples"]
    scenarios = CORPUS["validation_scenarios"]
    assert len({item["sample_id"] for item in samples}) == len(samples)
    assert len({item["scenario_id"] for item in scenarios}) == len(scenarios)
    sample_ids = {item["sample_id"] for item in samples}
    assert all(item["sample_id"] in sample_ids for item in scenarios)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  hello   world  ", "hello world"),
        ("I\nlove\tEnglish.", "I love English."),
        ("I love English\u3002", "I love English."),
        ("I don\u2019t know.", "I don't know."),
    ],
)
def test_VAL_NORM_002_normalization_is_deterministic(raw, expected):
    assert normalize_text(raw) == expected
    assert normalize_text(normalize_text(raw)) == expected


@pytest.mark.parametrize("case", CORPUS["validation_scenarios"], ids=lambda item: item["scenario_id"])
def test_DEC_001_DEC_002_full_decision_matrix(case):
    result = _decision_for_contract_case(case)
    assert result["level"] == case["expected_level"]
    assert result["warningTypes"] == case["expected_warning_types"]
    assert result["canSave"] is case["canSave"]
    assert result["canAnalyze"] is case["canAnalyze"]
    assert result["canPronounce"] is case["canPronounce"]


@pytest.mark.parametrize(
    ("text", "expected_category"),
    [
        ("because", "word"),
        ("give up", "phrase"),
        ("I love English.", "sentence"),
        ("This is the first sentence. This is the second sentence.", "paragraph"),
    ],
)
def test_VAL_CAT_001_category_boundaries(text, expected_category):
    assert validate_english(text)["category"] == expected_category


def test_VAL_HARD_001_hard_error_short_circuits_all_lexical_and_harper_calls():
    with (
        patch("app.services.validator.dictionary_available") as dictionary,
        patch("app.services.validator.get_dictionary_entry") as ecdict,
        patch("app.services.validator._get_symspell") as symspell,
        patch("app.services.validator.get_harper_evidence") as harper,
    ):
        result = validate_english("https://example.com")

    assert result["level"] == "error"
    dictionary.assert_not_called()
    ecdict.assert_not_called()
    symspell.assert_not_called()
    harper.assert_not_called()


def test_API_001_validation_response_has_contract_capabilities_and_warning_types():
    result = validate_english("because")
    assert set(result) >= {
        "level",
        "category",
        "normalizedText",
        "warnings",
        "errors",
        "evidence",
        "warningTypes",
        "canSave",
        "canAnalyze",
        "canPronounce",
    }


def test_TTS_001_word_phonetic_rule_is_explicitly_represented_by_contract_data():
    word_case = next(item for item in CORPUS["validation_scenarios"] if item["scenario_id"] == "VAL-PASS-001")
    assert word_case["phonetic_expectation"] == "ecdict_hit"
    assert word_case["canPronounce"] is True
