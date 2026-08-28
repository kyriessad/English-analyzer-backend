from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WARNING_TYPES = (
    "CONTENT_WARNING",
    "ADVISORY_WARNING",
    "SYSTEM_WARNING",
)


@dataclass(frozen=True)
class ValidationDecisionInput:
    hard_rule_errors: list[str]
    warnings: list[str]
    evidence: list[dict[str, Any]]
    detected_category: str
    requested_category: str | None
    normalized_text: str
    warning_types: list[str] | None = None


def _dedupe_warnings(warnings: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for warning in warnings:
        key = " ".join(str(warning).strip().lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped


def decide_validation(input_data: ValidationDecisionInput) -> dict[str, Any]:
    warnings = _dedupe_warnings(input_data.warnings)
    warning_types = [
        warning_type
        for warning_type in WARNING_TYPES
        if warning_type in (input_data.warning_types or [])
    ]

    if input_data.hard_rule_errors:
        level = "error"
    elif warnings:
        level = "warning"
    else:
        level = "pass"

    content_warning = "CONTENT_WARNING" in warning_types
    word_lexical_evidence = any(
        item.get("source") == "ecdict"
        and item.get("result") == "hit"
        and item.get("polarity") == "positive"
        for item in input_data.evidence
    ) or any(
        item.get("source") == "symspell"
        and item.get("result") == "exact"
        for item in input_data.evidence
    )

    if level == "error":
        can_save = can_analyze = can_pronounce = False
    else:
        can_save = True
        can_analyze = not content_warning
        if content_warning:
            can_pronounce = False
        elif input_data.detected_category == "word" and input_data.requested_category not in {
            "phrase",
            "sentence",
        }:
            can_pronounce = word_lexical_evidence
        else:
            can_pronounce = (
                input_data.detected_category in {"phrase", "sentence"}
                or input_data.requested_category in {"phrase", "sentence"}
            )

    return {
        "level": level,
        "category": input_data.detected_category,
        "normalizedText": input_data.normalized_text,
        "warnings": [] if level == "error" else warnings,
        "errors": input_data.hard_rule_errors,
        "evidence": input_data.evidence,
        "warningTypes": [] if level == "error" else warning_types,
        "canSave": can_save,
        "canAnalyze": can_analyze,
        "canPronounce": can_pronounce,
    }
