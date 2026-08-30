"""
英文检测规则中心
"""
import re
import unicodedata
from functools import lru_cache
from importlib import resources
from typing import Any

from app.services.ecdict_service import (
    dictionary_available,
    get_dictionary_entry,
    normalize_lexical_text,
)
from app.services.harper_service import get_harper_evidence
from app.services.validation_decision import ValidationDecisionInput, decide_validation

try:
    from symspellpy import SymSpell, Verbosity
except ImportError:  # pragma: no cover - handled gracefully at runtime
    SymSpell = None
    Verbosity = None


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
ENGLISH_LETTER_RE = re.compile(r"[A-Za-z]")
ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?(?:-[A-Za-z]+)*")
SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?=\s|$)")
ABBREVIATION_DOT_RE = re.compile(
    r"\b(?:[A-Za-z]\.){2,}|\b(?:e\.g|i\.e)\.|\b[A-Za-z]{1,4}\.",
    re.IGNORECASE,
)
REPEATED_OR_MIXED_PUNCTUATION_RE = re.compile(r"\?{3,}|!{3,}|\.{3,}|\?!|!\?")
RAW_HARD_RULE_CHARACTER_RE = re.compile(r"[/\\\x00-\x1F\x7F\u200B\u200C\u200D\uFEFF\u202A-\u202E]")
URL_ONLY_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
EMAIL_ONLY_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
HTML_ONLY_RE = re.compile(
    r"^(?:<!doctype\s+html[^>]*>|<([A-Za-z][\w:-]*)(?:\s[^<>]*)?>.*</\1>|<([A-Za-z][\w:-]*)(?:\s[^<>]*)?/>)$",
    re.IGNORECASE | re.DOTALL,
)
CODE_ONLY_RE = re.compile(
    r"""^(?:
        [A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\([^)]*\)\s*;?
        |(?:const|let|var|function|return|if|for|while|class|import|from|def|print|console)\b.*[;{}()]
        |.*[{}]\s*
    )$""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
FILESYSTEM_PATH_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]\S+|(?:\.{1,2}[\\/]|~[\\/]|/)\S+)$"
)
SPLIT_POSSESSIVE_APOSTROPHE_RE = re.compile(r"\b([A-Za-z]+s)\s+'\s+(?=[A-Za-z])")
SPLIT_APOSTROPHE_SUFFIX_RE = re.compile(r"\b([A-Za-z]+)\s*'\s+(s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
SPACED_APOSTROPHE_SUFFIX_RE = re.compile(r"\b([A-Za-z]+)\s+'\s*(s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
DOUBLE_PERIOD_RE = re.compile(r"(?<!\.)\.\.(?!\.)")
LIST_PUNCTUATION_NEEDS_SPACE_RE = re.compile(r"([,;:])([A-Za-z0-9])")
SENTENCE_PUNCTUATION_NEEDS_SPACE_RE = re.compile(r"([.!?])([A-Z])")

PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "？": "?",
        "！": "!",
        "；": ";",
        "：": ":",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "、": ",",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‘": "'",
        "’": "'",
        "`": "'",
        "｀": "'",
    }
)

PROPER_NOUN_WHITELIST = {
    "chatgpt",
    "openai",
    "tiktok",
    "youtube",
    "iphone",
    "deepl",
    "google",
    "microsoft",
}

CATEGORY_LABELS = {
    "word": "单词",
    "phrase": "短语",
    "sentence": "句子",
    "paragraph": "段落",
}

REQUESTED_CATEGORY_ALIASES = {
    "word": "word",
    "单词": "word",
    "phrase": "phrase",
    "短语": "phrase",
    "sentence": "sentence",
    "句子": "sentence",
    "paragraph": "paragraph",
    "段落": "paragraph",
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.translate(PUNCTUATION_TRANSLATION)
    value = value.replace("\u00a0", " ").replace("\u3000", " ")
    value = SPLIT_POSSESSIVE_APOSTROPHE_RE.sub(r"\1' ", value)
    value = SPACED_APOSTROPHE_SUFFIX_RE.sub(r"\1'\2", value)
    value = SPLIT_APOSTROPHE_SUFFIX_RE.sub(r"\1'\2", value)
    value = re.sub(r",{2,}", ",", value)
    value = DOUBLE_PERIOD_RE.sub(".", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;:?!)\]}])", r"\1", value)
    value = re.sub(r"([\[({])\s+", r"\1", value)
    value = re.sub(r"\s+([\])}])", r"\1", value)
    value = _normalize_punctuation_spacing(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _normalize_punctuation_spacing(text: str) -> str:
    def add_space_after_list_punctuation(match: re.Match[str]) -> str:
        punctuation = match.group(1)
        next_char = match.group(2)
        previous_char = match.string[match.start() - 1] if match.start() > 0 else ""

        if punctuation in {",", ":"} and previous_char.isdigit() and next_char.isdigit():
            return f"{punctuation}{next_char}"

        return f"{punctuation} {next_char}"

    def add_space_after_sentence_punctuation(match: re.Match[str]) -> str:
        punctuation = match.group(1)
        next_char = match.group(2)
        previous_char = match.string[match.start() - 1] if match.start() > 0 else ""

        if punctuation == "." and previous_char.isupper() and next_char.isupper():
            return f"{punctuation}{next_char}"

        return f"{punctuation} {next_char}"

    text = LIST_PUNCTUATION_NEEDS_SPACE_RE.sub(add_space_after_list_punctuation, text)
    return SENTENCE_PUNCTUATION_NEEDS_SPACE_RE.sub(add_space_after_sentence_punctuation, text)


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _get_english_tokens(text: str) -> list[str]:
    return ENGLISH_TOKEN_RE.findall(text)


def _has_chinese(text: str) -> bool:
    return bool(CHINESE_RE.search(text))


def _has_english(text: str) -> bool:
    return bool(ENGLISH_LETTER_RE.search(text))


def _has_latin_letter(text: str) -> bool:
    return any(
        unicodedata.category(char).startswith("L")
        and "LATIN" in unicodedata.name(char, "")
        for char in text
    )


def _is_all_digits(text: str) -> bool:
    compacted = _compact(text)
    return bool(compacted) and compacted.isdigit()


def _is_numeric_value_only(text: str) -> bool:
    compacted = _compact(text)
    return (
        bool(compacted)
        and not _has_latin_letter(compacted)
        and bool(re.search(r"\d", compacted))
        and bool(re.fullmatch(r"[0-9.,\-+%/:$¥￥]+", compacted))
    )


def _is_all_symbols(text: str) -> bool:
    return bool(text) and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text)


def _has_invalid_invisible(text: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in text)


def _has_raw_hard_rule_character(text: str) -> bool:
    return bool(RAW_HARD_RULE_CHARACTER_RE.search(text))


def _is_extreme_single_char_repeat(text: str) -> bool:
    compacted = _compact(text)
    return len(compacted) >= 12 and len(set(compacted.lower())) == 1 and compacted[0].isalpha()


@lru_cache(maxsize=1)
def _get_symspell():
    if SymSpell is None:
        return None

    symspell = SymSpell(max_dictionary_edit_distance=1, prefix_length=7)
    dictionary_path = resources.files("symspellpy").joinpath(
        "frequency_dictionary_en_82_765.txt"
    )
    if not symspell.load_dictionary(str(dictionary_path), term_index=0, count_index=1):
        return None
    return symspell


def _is_single_plain_lowercase_word(token: str) -> bool:
    return bool(re.fullmatch(r"[a-z]+", token))


def _should_check_spelling(
    normalized_text: str,
    category: str,
    tokens: list[str],
) -> bool:
    if category != "word":
        return False

    if len(tokens) != 1:
        return False

    token = tokens[0]
    if normalized_text != token:
        return False

    if token in PROPER_NOUN_WHITELIST:
        return False

    return _is_single_plain_lowercase_word(token)


def _get_symspell_evidence(
    normalized_text: str,
    category: str,
    tokens: list[str],
) -> dict[str, str | int]:
    if not _should_check_spelling(normalized_text, category, tokens):
        return {
            "source": "symspell",
            "type": "spelling",
            "result": "skipped",
            "polarity": "neutral",
        }

    token = tokens[0]
    lowered = token.lower()

    try:
        symspell = _get_symspell()
        if symspell is None or Verbosity is None:
            return {
                "source": "symspell",
                "type": "spelling",
                "result": "unavailable",
                "polarity": "neutral",
            }

        suggestions = symspell.lookup(
            lowered,
            Verbosity.CLOSEST,
            max_edit_distance=1,
            include_unknown=False,
        )
    except Exception:
        return {
            "source": "symspell",
            "type": "spelling",
            "result": "unavailable",
            "polarity": "neutral",
        }

    if not suggestions:
        return {
            "source": "symspell",
            "type": "spelling",
            "result": "no_suggestion",
            "polarity": "neutral",
        }

    best = suggestions[0]
    suggestion = str(best.term)
    distance = int(best.distance)
    if suggestion == lowered or distance == 0:
        return {
            "source": "symspell",
            "type": "spelling",
            "result": "exact",
            "polarity": "neutral",
        }

    if distance == 1:
        return {
            "source": "symspell",
            "type": "spelling",
            "result": "suggestion",
            "polarity": "warning",
            "suggestion": suggestion,
            "distance": distance,
        }

    return {
        "source": "symspell",
        "type": "spelling",
        "result": "no_suggestion",
        "polarity": "neutral",
    }


def _get_spelling_warning(evidence: dict[str, str | int], token: str) -> str | None:
    if evidence.get("source") != "symspell" or evidence.get("result") != "suggestion":
        return None
    suggestion = evidence.get("suggestion")
    if not suggestion:
        return None
    return f"拼写可能有误：{token}。你是不是想写 {suggestion}？"

def _get_format_warnings(normalized_text: str, has_english: bool) -> list[str]:
    return []


def _is_abbreviation_like(text: str) -> bool:
    """True if *text* looks like an abbreviation: U.S., e.g., Dr., i.e., etc.

    Criteria: no spaces, ends with '.', every dot-separated segment is 1–4 letters.
    """
    segments = [s for s in text.rstrip(".").split(".") if s]
    return bool(segments) and all(re.fullmatch(r"[A-Za-z]{1,4}", s) for s in segments)


def _count_sentence_units(text: str) -> int:
    without_abbreviations = ABBREVIATION_DOT_RE.sub(
        lambda match: match.group(0).replace(".", ""),
        text,
    )
    return len(SENTENCE_BOUNDARY_RE.findall(without_abbreviations))


def _normalize_requested_category(category: str | None) -> str | None:
    normalized = str(category or "").strip().lower()
    if not normalized or normalized == "auto":
        return None
    return REQUESTED_CATEGORY_ALIASES.get(normalized)


def _get_category_mismatch_warnings(
    requested_category: str | None,
    detected_category: str,
) -> list[str]:
    requested = _normalize_requested_category(requested_category)
    if not requested or requested == detected_category:
        return []
    label = CATEGORY_LABELS.get(detected_category)
    if not label:
        return []
    return [f"这段内容看起来更像{label}。"]


def _classify_text(text: str, tokens: list[str]) -> str:
    # No-space inputs are single-unit entries (word, abbreviation, alphanumeric term).
    # Accept them when every character is a valid word character AND the text has at
    # least one English letter.  This covers:
    #   COVID-19, 5G, B2B, GPT-4, well-known, e-mail  → word
    #   U.S., e.g., Dr., i.e.                          → word (abbreviation)
    # And rejects:
    #   #N/A, @@@, /, !!!  (non-word symbols present)  → unknown
    #   2024, 100-200, -50 (no English letter)         → unknown
    if not re.search(r"\s", text):
        if SENTENCE_END_RE.search(text) and _has_latin_letter(text):
            if text.endswith(".") and _is_abbreviation_like(text):
                return "word"
            return "sentence"
        if not re.fullmatch(r"[A-Za-z0-9.\-'']+", text):
            return "unknown"
        if not _has_latin_letter(text):
            return "unknown"
        # Abbreviations end with '.' and have short alpha segments — return "word"
        # immediately so SENTENCE_END_RE below does not fire on them.
        if text.endswith(".") and _is_abbreviation_like(text):
            return "word"
        if SENTENCE_END_RE.search(text):
            return "sentence"
        return "word"

    # Multi-word inputs: apply sentence heuristics on token count / trailing punctuation.
    token_count = len(tokens)
    sentence_units = _count_sentence_units(text)

    if sentence_units >= 2 and token_count >= 10:
        return "paragraph"

    if SENTENCE_END_RE.search(text):
        return "sentence"

    if token_count >= 6:
        return "sentence"

    if 2 <= token_count <= 5:
        return "phrase"

    if token_count == 1:
        return "word"

    return "unknown"


def _get_ecdict_evidence(normalized_text: str, category: str) -> list[dict[str, str]]:
    if category not in {"word", "phrase"}:
        return [{
            "source": "ecdict",
            "type": "lexical_match",
            "result": "skipped",
            "polarity": "neutral",
        }]

    try:
        if not dictionary_available():
            result = "unavailable"
        else:
            lookup_key = normalize_lexical_text(normalized_text)
            result = "hit" if get_dictionary_entry(lookup_key) is not None else "miss"
    except Exception:
        result = "unavailable"

    return [{
        "source": "ecdict",
        "type": "lexical_match",
        "result": result,
        "polarity": "positive" if result == "hit" else "neutral",
    }]


def validate_english(text: str, requested_category: str | None = None) -> dict[str, Any]:
    raw_text = str(text or "")
    if _has_raw_hard_rule_character(raw_text):
        return decide_validation(
            ValidationDecisionInput(
                hard_rule_errors=["English content contains forbidden control or path characters."],
                warnings=[],
                evidence=[],
                detected_category="unknown",
                requested_category=requested_category,
                normalized_text=raw_text,
                warning_types=[],
            )
        )

    normalized_text = _collapse_spaces(normalize_text(text))
    warnings: list[str] = []
    warning_types: list[str] = []
    errors: list[str] = []

    has_chinese = _has_chinese(normalized_text)
    has_english = _has_latin_letter(normalized_text)

    if not normalized_text:
        return decide_validation(
            ValidationDecisionInput(
                hard_rule_errors=["英文内容为空。"],
                warnings=[],
                evidence=[],
                detected_category="unknown",
                requested_category=requested_category,
                normalized_text="",
            )
        )

    if _has_invalid_invisible(normalized_text):
        errors.append("英文内容包含不可见或控制字符，请清理后再保存。")

    if has_chinese:
        errors.append("英文内容请只填写英文，不能包含中文字符。")

    if _is_numeric_value_only(normalized_text):
        errors.append("内容需要包含英文，不能只填写数字或数值。")

    if _is_all_symbols(normalized_text):
        errors.append("内容不能只有符号。")

    if not has_english:
        errors.append("内容需要包含英文。")

    if URL_ONLY_RE.fullmatch(normalized_text):
        errors.append("英文内容不能只填写网址。")

    if EMAIL_ONLY_RE.fullmatch(normalized_text):
        errors.append("英文内容不能只填写邮箱地址。")

    if HTML_ONLY_RE.fullmatch(normalized_text):
        errors.append("英文内容不能只填写 HTML 片段。")

    if CODE_ONLY_RE.fullmatch(normalized_text):
        errors.append("英文内容不能只填写代码片段。")

    if FILESYSTEM_PATH_ONLY_RE.fullmatch(normalized_text):
        errors.append("英文内容不能只填写文件路径。")

    if _is_extreme_single_char_repeat(normalized_text):
        errors.append("英文内容不能是明显的单字符重复垃圾。")

    if errors:
        return decide_validation(
            ValidationDecisionInput(
                hard_rule_errors=errors,
                warnings=warnings,
                evidence=[],
                detected_category="unknown",
                requested_category=requested_category,
                normalized_text=normalized_text,
                warning_types=warning_types,
            )
        )

    tokens = _get_english_tokens(normalized_text)
    category = _classify_text(normalized_text, tokens)

    if len(normalized_text) > 180:
        warnings.append("内容较长，已按段落处理，建议复习时拆成更短的卡片。")

    if len(normalized_text) > 180:
        warning_types.append("ADVISORY_WARNING")

    evidence = _get_ecdict_evidence(normalized_text, category)
    ecdict_hit = any(
        item.get("source") == "ecdict" and item.get("result") == "hit"
        for item in evidence
    )
    if ecdict_hit:
        symspell_evidence: dict[str, str | int] = {
            "source": "symspell",
            "type": "spelling",
            "result": "skipped",
            "polarity": "neutral",
        }
    else:
        symspell_evidence = _get_symspell_evidence(normalized_text, category, tokens)
    evidence.append(symspell_evidence)
    harper_evidence, harper_warnings = get_harper_evidence(
        normalized_text,
        category,
    )
    evidence.extend(harper_evidence)

    format_warnings = _get_format_warnings(normalized_text, has_english)
    warnings.extend(format_warnings)
    if format_warnings:
        warning_types.append("ADVISORY_WARNING")
    spelling_warning = _get_spelling_warning(
        symspell_evidence,
        tokens[0] if tokens else normalized_text,
    )
    if spelling_warning:
        warnings.append(spelling_warning)
    harper_blocks_ai = any(
        item.get("source") == "harper"
        and item.get("type") in {"grammar", "usage"}
        and item.get("polarity") == "warning"
        for item in harper_evidence
    )
    harper_punctuation_only = (
        bool(harper_warnings)
        and not harper_blocks_ai
        and all(
            item.get("source") != "harper"
            or item.get("type") == "punctuation"
            or item.get("polarity") != "warning"
            for item in harper_evidence
        )
    )
    if not harper_punctuation_only:
        warnings.extend(harper_warnings)
    if spelling_warning:
        warning_types.append("CONTENT_WARNING")
    if harper_blocks_ai:
        warning_types.append("CONTENT_WARNING")
    if any(
        item.get("source") == "harper" and item.get("result") == "unavailable"
        for item in harper_evidence
    ):
        warnings.append("Harper unavailable")
        warning_types.append("SYSTEM_WARNING")
    mismatch_warnings = _get_category_mismatch_warnings(requested_category, category)
    warnings.extend(mismatch_warnings)
    if mismatch_warnings:
        warning_types.append("ADVISORY_WARNING")

    return decide_validation(
        ValidationDecisionInput(
            hard_rule_errors=[],
            warnings=warnings,
            evidence=evidence,
            detected_category=category,
            requested_category=requested_category,
            normalized_text=normalized_text,
            warning_types=warning_types,
        )
    )
