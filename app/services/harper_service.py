"""Fail-open warning provider for an official harper.js LocalLinter sidecar."""
from typing import Any

import httpx

from app.core.config import settings


SUPPORTED_CATEGORIES = {"phrase", "sentence", "paragraph"}


def _service_evidence(result: str) -> dict[str, str]:
    return {
        "source": "harper",
        "type": "service",
        "result": result,
        "polarity": "neutral",
    }


def _lint_type(lint: dict[str, Any]) -> str:
    kind = str(lint.get("kind", "")).lower()
    if any(
        value in kind
        for value in ("punct", "formatting", "capitalization", "typographical")
    ):
        return "punctuation"
    if any(value in kind for value in ("spell", "typo", "malapropism", "eggcorn")):
        return "spelling"
    if any(value in kind for value in ("style", "usage", "nonstandard", "readability")):
        return "usage"
    if any(value in kind for value in ("grammar", "agreement", "wordorder")):
        return "grammar"
    return "other"


def _lint_evidence(lint: dict[str, Any]) -> dict[str, Any]:
    replacements = lint.get("replacements", [])
    if not isinstance(replacements, list):
        replacements = []
    return {
        "source": "harper",
        "type": _lint_type(lint),
        "result": "lint",
        "polarity": "warning",
        "message": str(lint.get("message", "")),
        "offset": int(lint.get("offset", 0)),
        "length": int(lint.get("length", 0)),
        "replacements": [str(value) for value in replacements],
    }


def get_harper_evidence(
    text: str,
    category: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if category not in SUPPORTED_CATEGORIES:
        return [_service_evidence("skipped")], []
    if not settings.harper_enabled:
        return [_service_evidence("skipped")], []

    try:
        response = httpx.post(
            f"{settings.harper_base_url.rstrip('/')}/lint",
            json={"text": text, "language": "plaintext"},
            timeout=settings.harper_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        lints = payload.get("lints") if isinstance(payload, dict) else None
        if not isinstance(lints, list) or any(
            not isinstance(item, dict) for item in lints
        ):
            raise ValueError("invalid Harper response")
        evidence = [_lint_evidence(item) for item in lints]
    except Exception:
        return [_service_evidence("unavailable")], []

    if not evidence:
        return [_service_evidence("no_lint")], []
    warnings = [
        f"Harper {item['type']}: {item['message']}"
        for item in evidence
        if item["message"]
    ]
    return evidence, warnings
