from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
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
@dataclass(frozen=True)
class EcdictSchema:
    table_name: str
    word_column: str
    phonetic_column: str
    translation_column: str | None
    pos_column: str | None
    frq_column: str | None
    bnc_column: str | None


@dataclass(frozen=True)
class EcdictEntry:
    word: str
    translation: str | None
    meanings: tuple[str, ...]
    pos: str | None
    frq: int | None
    bnc: int | None


def _optional_column(columns: list[str], *names: str) -> str | None:
    for name in names:
        if name in columns:
            return name
    return None


@lru_cache(maxsize=1)
def _resolve_schema() -> EcdictSchema | None:
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
                    return EcdictSchema(
                        table_name=table_name,
                        word_column="word",
                        phonetic_column=phonetic_column,
                        translation_column=translation_column,
                        pos_column=_optional_column(columns, "pos"),
                        frq_column=_optional_column(columns, "frq"),
                        bnc_column=_optional_column(columns, "bnc"),
                    )

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

    table_name = schema.table_name
    word_column = schema.word_column
    phonetic_column = schema.phonetic_column
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

    table_name = schema.table_name
    word_column = schema.word_column
    translation_column = schema.translation_column
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


def _to_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_pos(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    tokens = re.findall(r"[a-z]+", raw)
    if not tokens:
        return None
    aliases = {
        "noun": "n",
        "verb": "v",
        "adjective": "adj",
        "adj": "adj",
        "adv": "adv",
        "adverb": "adv",
        "prep": "prep",
        "preposition": "prep",
        "conj": "conj",
        "pron": "pron",
        "det": "det",
        "num": "num",
        "interj": "int",
        "int": "int",
    }
    priority = ("n", "v", "adj", "adv", "prep", "conj", "pron", "det", "num", "int")
    normalized = [aliases.get(token, token) for token in tokens]
    for item in priority:
        if item in normalized:
            return item
    return normalized[0][:32]


@lru_cache(maxsize=4096)
def get_dictionary_entry(text: str) -> EcdictEntry | None:
    started = time.perf_counter()
    normalized = normalize_lexical_text(text)
    if not normalized:
        record_operation_result("ecdict", "get_dictionary_entry", "miss", time.perf_counter() - started)
        return None

    schema = _resolve_schema()
    if schema is None:
        record_operation_result("ecdict", "get_dictionary_entry", "unavailable", time.perf_counter() - started)
        return None

    columns = [schema.word_column]
    if schema.translation_column:
        columns.append(schema.translation_column)
    if schema.pos_column:
        columns.append(schema.pos_column)
    if schema.frq_column:
        columns.append(schema.frq_column)
    if schema.bnc_column:
        columns.append(schema.bnc_column)

    quoted_table = _quote_identifier(schema.table_name)
    quoted_word = _quote_identifier(schema.word_column)
    select_sql = ", ".join(_quote_identifier(column) for column in columns)
    db_path = Path(settings.ecdict_db_path)

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            f"SELECT {select_sql} FROM {quoted_table} WHERE lower({quoted_word}) = ? LIMIT 1",
            (normalized,),
        ).fetchone()

    if not row:
        record_operation_result("ecdict", "get_dictionary_entry", "miss", time.perf_counter() - started)
        return None

    values = dict(zip(columns, row, strict=False))
    translation = values.get(schema.translation_column) if schema.translation_column else None
    entry = EcdictEntry(
        word=str(values.get(schema.word_column) or normalized),
        translation=str(translation) if translation is not None else None,
        meanings=tuple(_translation_candidates(translation)),
        pos=normalize_pos(
            str(values.get(schema.pos_column) or "")
            if schema.pos_column
            else str(translation or "")
        ),
        frq=_to_int(values.get(schema.frq_column)) if schema.frq_column else None,
        bnc=_to_int(values.get(schema.bnc_column)) if schema.bnc_column else None,
    )
    record_operation_result("ecdict", "get_dictionary_entry", "hit", time.perf_counter() - started)
    return entry


def get_dictionary_distractor_entries(
    *,
    exclude_words: set[str] | None = None,
    exclude_meanings: set[str] | None = None,
    pos: str | None = None,
    frq: int | None = None,
    bnc: int | None = None,
    limit: int = 80,
) -> tuple[EcdictEntry, ...]:
    started = time.perf_counter()
    schema = _resolve_schema()
    if schema is None or not schema.translation_column:
        record_operation_result("ecdict", "get_dictionary_distractors", "unavailable", time.perf_counter() - started)
        return ()

    excluded_words = {normalize_lexical_text(word) for word in (exclude_words or set()) if normalize_lexical_text(word)}
    excluded_meanings = {str(meaning).strip() for meaning in (exclude_meanings or set()) if str(meaning).strip()}
    normalized_pos = normalize_pos(pos)

    columns = [schema.word_column, schema.translation_column]
    if schema.pos_column:
        columns.append(schema.pos_column)
    if schema.frq_column:
        columns.append(schema.frq_column)
    if schema.bnc_column:
        columns.append(schema.bnc_column)

    quoted_table = _quote_identifier(schema.table_name)
    quoted_translation = _quote_identifier(schema.translation_column)
    filters = [f"{quoted_translation} IS NOT NULL", f"trim({quoted_translation}) <> ''"]
    params: list[object] = []

    if excluded_words:
        quoted_word = _quote_identifier(schema.word_column)
        placeholders = ", ".join("?" for _ in excluded_words)
        filters.append(f"lower({quoted_word}) NOT IN ({placeholders})")
        params.extend(sorted(excluded_words))

    order_clause = "random()"
    if schema.frq_column and frq is not None:
        order_clause = f"abs(coalesce({_quote_identifier(schema.frq_column)}, 999999) - ?), random()"
        params.append(frq)
    elif schema.bnc_column and bnc is not None:
        order_clause = f"abs(coalesce({_quote_identifier(schema.bnc_column)}, 999999) - ?), random()"
        params.append(bnc)

    select_sql = ", ".join(_quote_identifier(column) for column in columns)
    sql = (
        f"SELECT {select_sql} FROM {quoted_table} "
        f"WHERE {' AND '.join(filters)} "
        f"ORDER BY {order_clause} LIMIT ?"
    )
    params.append(max(limit * 4, limit, 20))

    db_path = Path(settings.ecdict_db_path)
    entries: list[EcdictEntry] = []
    seen_meanings = set(excluded_meanings)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(sql, params).fetchall()

    for row in rows:
        values = dict(zip(columns, row, strict=False))
        translation = values.get(schema.translation_column)
        entry_pos = normalize_pos(
            str(values.get(schema.pos_column) or "")
            if schema.pos_column
            else str(translation or "")
        )
        if normalized_pos and entry_pos and entry_pos != normalized_pos:
            continue
        meanings = tuple(_translation_candidates(translation))
        first_meaning = next((meaning for meaning in meanings if meaning and meaning not in seen_meanings), None)
        if not first_meaning:
            continue
        seen_meanings.add(first_meaning)
        entries.append(
            EcdictEntry(
                word=str(values.get(schema.word_column) or ""),
                translation=str(values.get(schema.translation_column) or ""),
                meanings=(first_meaning,),
                pos=entry_pos,
                frq=_to_int(values.get(schema.frq_column)) if schema.frq_column else None,
                bnc=_to_int(values.get(schema.bnc_column)) if schema.bnc_column else None,
            )
        )
        if len(entries) >= limit:
            break

    record_operation_result(
        "ecdict",
        "get_dictionary_distractors",
        "hit" if entries else "miss",
        time.perf_counter() - started,
    )
    return tuple(entries)


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
