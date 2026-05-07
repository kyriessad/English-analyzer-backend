"""
英文检测规则中心
"""
import re
import unicodedata
from typing import Any

try:
    from spellchecker import SpellChecker
except ImportError:  # pragma: no cover - handled gracefully at runtime
    SpellChecker = None


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
ENGLISH_LETTER_RE = re.compile(r"[A-Za-z]")
ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?(?:-[A-Za-z]+)*")
SENTENCE_END_RE = re.compile(r"[.!?]\s*$")
REPEATED_OR_MIXED_PUNCTUATION_RE = re.compile(r"\?{3,}|!{3,}|\.{3,}|\?!|!\?")
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

_spellchecker: SpellChecker | None = None


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


def _is_all_digits(text: str) -> bool:
    compacted = _compact(text)
    return bool(compacted) and compacted.isdigit()


def _is_numeric_value_only(text: str) -> bool:
    compacted = _compact(text)
    return (
        bool(compacted)
        and not _has_english(compacted)
        and bool(re.search(r"\d", compacted))
        and bool(re.fullmatch(r"[0-9.,\-+%/:$¥￥]+", compacted))
    )


def _is_all_symbols(text: str) -> bool:
    return bool(text) and not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text)


def _get_spellchecker() -> SpellChecker | None:
    global _spellchecker

    if SpellChecker is None:
        return None

    if _spellchecker is None:
        _spellchecker = SpellChecker(language="en")

    return _spellchecker


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


def _get_spelling_warnings(
    normalized_text: str,
    category: str,
    tokens: list[str],
) -> list[str]:
    if not _should_check_spelling(normalized_text, category, tokens):
        return []

    token = tokens[0]

    spellchecker = _get_spellchecker()
    if spellchecker is None:
        return []

    lowered = token.lower()
    if lowered not in spellchecker.unknown([lowered]):
        return []

    correction = spellchecker.correction(lowered)
    if correction and correction != lowered:
        return [f"拼写疑似有误：{token}。你是不是想写 “{correction}”？"]

    return [f"拼写疑似有误：{token}。如果这是人名、地名、品牌名或专有名词，可以继续保存。"]


def _get_format_warnings(normalized_text: str, has_english: bool) -> list[str]:
    if not has_english:
        return []

    warnings: list[str] = []

    if REPEATED_OR_MIXED_PUNCTUATION_RE.search(normalized_text):
        warnings.append("内容中有连续或混合标点，建议确认是否为有意输入。")

    return warnings


def _classify_text(text: str, tokens: list[str]) -> str:
    if len(text) > 180:
        return "paragraph"

    if _has_chinese(text) and _has_english(text):
        return "unknown"

    if not re.search(r"\s", text) and re.search(r"[\d-]", text):
        return "unknown"

    token_count = len(tokens)

    if SENTENCE_END_RE.search(text):
        return "sentence"

    if token_count >= 6:
        return "sentence"

    if 2 <= token_count <= 5:
        return "phrase"

    if token_count == 1:
        return "word"

    return "unknown"


def validate_english(text: str) -> dict[str, Any]:
    normalized_text = _collapse_spaces(normalize_text(text))
    warnings: list[str] = []
    errors: list[str] = []

    has_chinese = _has_chinese(normalized_text)
    has_english = _has_english(normalized_text)
    tokens = _get_english_tokens(normalized_text)
    category = _classify_text(normalized_text, tokens)

    if not normalized_text:
        return {
            "level": "error",
            "category": "unknown",
            "normalizedText": "",
            "warnings": [],
            "errors": ["英文内容为空。"],
        }

    if len(normalized_text) > 500:
        errors.append("英文内容超过 500 字符，请拆分后再保存。")

    if has_chinese and not has_english:
        errors.append("内容需要包含英文，不能只有中文。")

    if _is_numeric_value_only(normalized_text):
        errors.append("内容需要包含英文，不能只填写数字或数值。")

    if _is_all_symbols(normalized_text):
        errors.append("内容不能只有符号。")

    if errors:
        return {
            "level": "error",
            "category": category if len(normalized_text) > 180 else "unknown",
            "normalizedText": normalized_text,
            "warnings": [],
            "errors": errors,
        }

    if has_chinese and has_english:
        warnings.append("内容包含中文，建议确认卡片英文内容是否需要拆分。")

    if 180 < len(normalized_text) <= 500:
        warnings.append("内容较长，已按段落处理，建议复习时拆成更短的卡片。")

    warnings.extend(_get_format_warnings(normalized_text, has_english))
    warnings.extend(_get_spelling_warnings(normalized_text, category, tokens))

    return {
        "level": "warning" if warnings else "pass",
        "category": category,
        "normalizedText": normalized_text,
        "warnings": warnings,
        "errors": [],
    }
