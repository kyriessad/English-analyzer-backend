from __future__ import annotations

import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.observability.operations import record_operation_result


_EDGE_PUNCT_RE = re.compile(
    "^[\\s\\\"'\\u201c\\u201d\\u2018\\u2019.,!?;:()\\[\\]{}<>]+"
    "|[\\s\\\"'\\u201c\\u201d\\u2018\\u2019.,!?;:()\\[\\]{}<>]+$"
)

# Word-like token: letters, digits, apostrophes (for contractions like don't),
# hyphens within words (well-known), and dots within abbreviations (U.S.)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.\\-]*[A-Za-z0-9]|[A-Za-z0-9]")


def normalize_lexical_text(text: str) -> str:
    normalized = _EDGE_PUNCT_RE.sub("", str(text or "").strip().lower())
    return re.sub(r"\s+", " ", normalized)


def _clean_phonetic(value: str | None) -> str | None:
    phonetic = str(value or "").strip()
    if not phonetic:
        return None

    return phonetic.strip().strip("/").strip("[]").strip() or None


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@lru_cache(maxsize=1)
def _resolve_schema() -> tuple[str, str, str, str | None] | None:
    db_path = Path(settings.ecdict_db_path)
    if not db_path.is_file():
        return None

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for (table_name,) in table_rows:
            quoted_table = _quote_identifier(table_name)
            columns = [
                row[1]
                for row in conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            ]
            if "word" not in columns:
                continue

            for phonetic_column in ("phonetic", "phonetic_us", "phonetic_uk"):
                if phonetic_column in columns:
                    translation_column = "translation" if "translation" in columns else None
                    return table_name, "word", phonetic_column, translation_column

    return None


@lru_cache(maxsize=4096)
def get_phonetic(text: str) -> str | None:
    """Look up phonetic for the exact full text (single word or known compound phrase)."""
    started = time.perf_counter()
    result = "miss"
    normalized = normalize_lexical_text(text)
    if not normalized:
        record_operation_result("ecdict", "get_phonetic", result, time.perf_counter() - started)
        return None

    schema = _resolve_schema()
    if schema is None:
        record_operation_result("ecdict", "get_phonetic", "unavailable", time.perf_counter() - started)
        return None

    table_name, word_column, phonetic_column, _ = schema
    db_path = Path(settings.ecdict_db_path)
    quoted_table = _quote_identifier(table_name)
    quoted_word = _quote_identifier(word_column)
    quoted_phonetic = _quote_identifier(phonetic_column)

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            f"SELECT {quoted_phonetic} FROM {quoted_table} "
            f"WHERE lower({quoted_word}) = ? LIMIT 1",
            (normalized,),
        ).fetchone()

    if not row:
        record_operation_result("ecdict", "get_phonetic", result, time.perf_counter() - started)
        return None

    phonetic = _clean_phonetic(row[0])
    result = "hit" if phonetic else "miss"
    record_operation_result("ecdict", "get_phonetic", result, time.perf_counter() - started)
    return phonetic


def _translation_candidates(value: str | None) -> list[str]:
    candidates: list[str] = []
    for line in str(value or "").splitlines():
        for part in re.split(r"[,，;；、]", line):
            candidate = re.sub(r"^[A-Za-z]{1,8}\.\s*", "", part.strip())
            candidate = re.sub(r"^\[[^\]]+\]\s*", "", candidate).strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


@lru_cache(maxsize=4096)
def get_dictionary_translations(text: str) -> tuple[str, ...]:
    """Return concise Chinese meanings for an exact ECDICT entry."""
    started = time.perf_counter()
    normalized = normalize_lexical_text(text)
    if not normalized:
        record_operation_result("ecdict", "get_dictionary_translations", "miss", time.perf_counter() - started)
        return ()

    schema = _resolve_schema()
    if schema is None:
        record_operation_result("ecdict", "get_dictionary_translations", "unavailable", time.perf_counter() - started)
        return ()

    table_name, word_column, _, translation_column = schema
    if not translation_column:
        return ()

    db_path = Path(settings.ecdict_db_path)
    quoted_table = _quote_identifier(table_name)
    quoted_word = _quote_identifier(word_column)
    quoted_translation = _quote_identifier(translation_column)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            f"SELECT {quoted_translation} FROM {quoted_table} "
            f"WHERE lower({quoted_word}) = ? LIMIT 1",
            (normalized,),
        ).fetchone()

    translations = tuple(_translation_candidates(row[0] if row else None))
    record_operation_result(
        "ecdict",
        "get_dictionary_translations",
        "hit" if translations else "miss",
        time.perf_counter() - started,
    )
    return translations


def get_dictionary_translation(
    text: str,
    context_translation: str | None = None,
) -> str | None:
    """Choose the dictionary sense used by a generated translated example."""
    candidates = get_dictionary_translations(text)
    if not candidates:
        return None

    context = str(context_translation or "").strip()
    if context:
        matches = [candidate for candidate in candidates if candidate in context]
        if matches:
            return max(matches, key=len)
        return None
    return candidates[0]


def _extract_word_tokens(text: str) -> list[str]:
    """Extract word-like tokens from text, skipping pure punctuation/whitespace.

    Preserves contractions (don't) and hyphenated words (well-known).
    Does NOT generate fake phonetics for non-word tokens.
    """
    return _WORD_TOKEN_RE.findall(text)


def get_word_phonetics(text: str) -> list[dict]:
    """Look up phonetics for each word token in the text.

    Returns a list of {word, phonetic} dicts. Words without a dictionary entry
    have phonetic=null so the frontend can display the word itself.
    Single-word input that has a full-phrase match is NOT returned here;
    callers should check get_phonetic() first.
    """
    normalized = normalize_lexical_text(text)
    if not normalized:
        return []

    tokens = _extract_word_tokens(normalized)
    if not tokens:
        return []

    # For single-word input, get_phonetic() should be used instead.
    # But still return word phonetics as a fallback for consistency.
    result = []
    seen: set[str] = set()
    for token in tokens:
        token_lower = token.lower()
        if token_lower in seen:
            continue
        seen.add(token_lower)
        phonetic = get_phonetic(token_lower)
        result.append({
            "word": token_lower,
            "phonetic": phonetic,
        })

    return result


def dictionary_available() -> bool:
    return _resolve_schema() is not None
