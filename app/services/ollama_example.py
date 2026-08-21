"""
Generate English example sentences via local Ollama.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from app.core.config import settings
from app.observability.operations import observed_operation
from app.providers.argos_translator import ArgosTranslator
from app.services.hunyuan_example import _text_in_sentence
from app.services.request_reliability import StreamCancelController

logger = logging.getLogger(__name__)

OLLAMA_NUM_PREDICT = 512
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

EXPRESSION_TYPES = {
    "literal",
    "idiom",
    "slang",
    "phrasal_verb",
    "fixed_expression",
    "colloquial",
    "polysemy",
}


class _OllamaDeadlineExpired(Exception):
    pass


@dataclass(frozen=True)
class _AttemptResult:
    sentence: str | None
    translation: str | None
    fail_reason: str | None = None
    retryable: bool = False
    meaning: str | None = None
    synonyms: list[dict] | None = None
    similar_phrases: list[dict] | None = None
    expression_type: str | None = None
    alternative_meanings: list[dict] | None = None
    usage_scenario: str | None = None
    dialogue: dict | None = None


def _ollama_url() -> str:
    return f"{settings.ollama_base_url.rstrip('/')}/api/generate"


def _json_schema_format() -> dict[str, Any]:
    pair_item = {
        "type": "object",
        "properties": {
            "english": {"type": "string"},
            "chinese": {"type": "string"},
        },
        "required": ["english", "chinese"],
    }
    alt_item = {
        "type": "object",
        "properties": {
            "meaning": {"type": "string"},
            "type": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["meaning"],
    }
    dialogue_item = {
        "type": "object",
        "properties": {
            "english": {"type": "array", "items": {"type": "string"}},
            "chinese": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["english", "chinese"],
    }
    return {
        "type": "object",
        "properties": {
            "meaning": {"type": "string"},
            "expressionType": {"type": "string"},
            "alternativeMeanings": {"type": "array", "items": alt_item},
            "usageScenario": {"type": "string"},
            "exampleSentence": {"type": "string"},
            "exampleTranslation": {"type": "string"},
            "dialogue": dialogue_item,
            "synonyms": {"type": "array", "items": pair_item},
            "similarPhrases": {"type": "array", "items": pair_item},
        },
        "required": ["exampleSentence", "exampleTranslation"],
    }


def _strip_outer_code_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```") or not text.endswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) < 3:
        return text
    if not lines[0].strip().startswith("```") or lines[-1].strip() != "```":
        return text
    return "\n".join(lines[1:-1]).strip()


def _looks_like_numbered_list(sentence: str) -> bool:
    text = sentence.strip()
    if re.search(r"(^|\n)\s*\d+[\.)]\s+", text):
        return True
    return bool(re.search(r"\n\s*[-*]\s+", text))


def _word_count(sentence: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", sentence))


def _bare_normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)*", text.lower()))


_SENTENCE_END_STRIP_RE = re.compile(r"[.!?]+\s*$")


def _match_target(text: str) -> str:
    """Strip trailing sentence punctuation from multi-word targets.

    A sentence like "You killed it." must be matchable against an example that
    embeds it naturally ("You killed it on stage!") rather than requiring the
    period to appear verbatim. Single-word targets (e.g. abbreviations "Dr.")
    keep their punctuation.
    """
    cleaned = str(text or "").strip()
    if " " not in cleaned:
        return cleaned
    return _SENTENCE_END_STRIP_RE.sub("", cleaned).strip()


def _is_valid_chinese_translation(text: str | None) -> bool:
    clean_text = str(text or "").strip()
    return bool(clean_text and _CJK_RE.search(clean_text))


def _normalize_pair_list(value: Any) -> list[dict]:
    """Normalize a JSON list of {english, chinese} (or plain strings) into clean pairs."""
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value:
        english = ""
        chinese = ""
        if isinstance(item, dict):
            english = str(item.get("english") or "").strip()
            chinese = str(item.get("chinese") or "").strip()
        elif isinstance(item, str):
            english = item.strip()
        if not english:
            continue
        result.append({"english": english, "chinese": chinese})
        if len(result) >= 4:
            break
    return result


def _normalize_expression_type(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in EXPRESSION_TYPES else "literal"


def _normalize_alternative_meanings(value: Any) -> list[dict]:
    """Normalize a JSON list of {meaning, type, note} into at most 2 clean items."""
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        meaning = str(item.get("meaning") or "").strip()
        if not meaning or not _CJK_RE.search(meaning):
            continue
        note = str(item.get("note") or "").strip()
        result.append(
            {
                "meaning": meaning,
                "type": _normalize_expression_type(item.get("type")),
                "note": note,
            }
        )
        if len(result) >= 2:
            break
    return result


def _normalize_usage_scenario(value: Any) -> str:
    cleaned = str(value or "").strip()
    return cleaned if _CJK_RE.search(cleaned) else ""


def _normalize_dialogue(value: Any) -> dict:
    """Normalize a dialogue object into {english: [...], chinese: [...]}, up to 3 turns."""
    empty = {"english": [], "chinese": []}
    if not isinstance(value, dict):
        return empty

    def _lines(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            line = str(item or "").strip()
            if line:
                result.append(line)
            if len(result) >= 3:
                break
        return result

    english = _lines(value.get("english"))
    chinese = _lines(value.get("chinese"))
    # A dialogue is only useful when both sides have at least one line.
    if not english or not chinese:
        return empty
    return {"english": english, "chinese": chinese}


def _validate_example(text: str, sentence: str) -> str | None:
    clean_text = str(text or "").strip()
    clean_sentence = str(sentence or "").strip()

    if not clean_sentence:
        return "missing_example_sentence"
    if _bare_normalized(clean_sentence) == _bare_normalized(clean_text):
        return "example_validation_failed"
    if _looks_like_numbered_list(clean_sentence):
        return "example_validation_failed"
    if _word_count(clean_sentence) < 3:
        return "example_validation_failed"
    if not _text_in_sentence(_match_target(clean_text), clean_sentence, allow_inflection=True):
        return "example_validation_failed"
    return None


def _normalize_analysis_data(text: str, data: dict[str, Any]) -> _AttemptResult:
    """Parse + normalize a full JSON analysis object into an ``_AttemptResult``.

    Shared by the non-streaming and streaming paths so the final result is
    normalized and validated exactly the same way in both.
    """
    sentence = str(data.get("exampleSentence") or "").strip()
    translation = str(data.get("exampleTranslation") or "").strip()
    meaning = str(data.get("meaning") or "").strip()
    synonyms = _normalize_pair_list(data.get("synonyms"))
    similar_phrases = _normalize_pair_list(data.get("similarPhrases"))
    expression_type = _normalize_expression_type(data.get("expressionType"))
    alternative_meanings = _normalize_alternative_meanings(data.get("alternativeMeanings"))
    usage_scenario = _normalize_usage_scenario(data.get("usageScenario"))
    dialogue = _normalize_dialogue(data.get("dialogue"))
    if not sentence:
        logger.warning("[ollama][diag] fail_reason=missing_example_sentence")
        return _AttemptResult(None, None, "missing_example_sentence", True)

    validation_error = _validate_example(text, sentence)
    if validation_error:
        logger.warning(
            "[ollama][diag] fail_reason=%s | sentence=%r",
            validation_error,
            sentence[:120],
        )
        return _AttemptResult(None, None, validation_error, True)

    logger.info("[ollama][diag] pass | sentence=%r", sentence[:120])
    return _AttemptResult(
        sentence,
        translation or None,
        None,
        False,
        meaning=meaning or None,
        synonyms=synonyms,
        similar_phrases=similar_phrases,
        expression_type=expression_type,
        alternative_meanings=alternative_meanings,
        usage_scenario=usage_scenario or None,
        dialogue=dialogue or None,
    )


def _assemble_analysis_result(text: str, result: _AttemptResult) -> dict[str, Any]:
    """Assemble the final analysis dict from a normalized ``_AttemptResult``.

    Mirrors the tail of ``generate_analysis_with_ollama`` (Argos translation
    fallback + meaning validation) so the streaming path produces the identical
    dict shape.
    """
    translation = result.translation
    if not _is_valid_chinese_translation(translation):
        if translation:
            logger.warning("[ollama][diag] fail_reason=qwen_example_translation_no_chinese")
        else:
            logger.warning("[ollama][diag] fail_reason=qwen_example_translation_missing")
        fallback_translation = ArgosTranslator().translate_to_zh(result.sentence)
        translation = fallback_translation

    meaning = result.meaning if _is_valid_chinese_translation(result.meaning) else None

    return {
        "meaning": meaning,
        "expressionType": result.expression_type or "literal",
        "alternativeMeanings": result.alternative_meanings or [],
        "usageScenario": result.usage_scenario or "",
        "exampleSentence": result.sentence,
        "exampleTranslation": translation,
        "dialogue": result.dialogue or {"english": [], "chinese": []},
        "synonyms": result.synonyms or [],
        "similarPhrases": result.similar_phrases or [],
    }


def _build_prompts(text: str, chinese_meaning: str | None, *, strict_retry: bool) -> tuple[str, str]:
    target = str(text or "").strip()
    is_phrase = len(target.split()) > 1
    label = "phrase" if is_phrase else "word"
    meaning_hint = f"\nChinese meaning hint: {chinese_meaning}" if chinese_meaning else ""
    stricter = (
        "\nThis is a retry. Be stricter: return only the JSON object, and make sure "
        "exampleSentence contains the target exactly as written."
        if strict_retry
        else ""
    )

    system_prompt = (
        "You are an English expression analyzer for Chinese-speaking learners. "
        "You explain what an English word or phrase usually means, not translate a sentence word by word. "
        "Do not think step by step. Return only valid JSON, no markdown, no notes."
    )
    match_target = _match_target(target)
    if is_phrase:
        target_rule = (
            f'The exampleSentence should contain the complete phrase "{match_target}" as contiguous words. '
            "Do not split the phrase across different parts of the sentence."
        )
    else:
        target_rule = (
            f'The exampleSentence should contain the word "{match_target}". '
            "Common inflections are acceptable only when they sound natural."
        )

    user_prompt = (
        f'Analyze this English {label} for a Chinese learner: "{target}".'
        f"{meaning_hint}\n"
        "Requirements:\n"
        "1. meaning: the single most common, most worth-learning Simplified-Chinese meaning. "
        "If this is an idiom, slang, phrasal verb, or fixed expression, use its figurative/idiomatic meaning as the primary meaning (never the literal reading).\n"
        "2. expressionType: exactly one of literal / idiom / slang / phrasal_verb / fixed_expression / colloquial / polysemy. "
        "Pick the single most useful category; literal means an ordinary, non-figurative expression.\n"
        "3. alternativeMeanings: 0 to 2 OTHER common meanings worth knowing, as a JSON array of "
        '{"meaning": "...", "type": "...", "note": "..."}. '
        "type uses the same enum. note is one short Chinese sentence explaining when/why this sense is used. "
        "Return [] if there is no other common, useful meaning — do not invent rare, historical or obscure senses. "
        "If unsure, return [] rather than guessing.\n"
        "4. No context is given, so never claim to know the speaker's exact intent; you are listing what this expression can commonly mean.\n"
        "5. usageScenario: one short, natural Simplified-Chinese sentence (not a paragraph) describing a typical situation where a native speaker would use this expression with the primary meaning you chose. "
        'For example "朋友邀请你一起吃饭、出去玩或参加活动时。". Return "" if none fits.\n'
        "6. exampleSentence: one natural daily-life English sentence, 6 to 18 English words, that clearly demonstrates the primary meaning you chose. "
        "If the primary meaning is figurative/slang/idiomatic, the example MUST use that sense, not the literal one.\n"
        f"7. {target_rule}\n"
        "8. exampleTranslation: Simplified Chinese, must match exampleSentence.\n"
        "9. dialogue: a very short 2-turn (max 3-turn) natural English dialogue that shows how the expression is used in real speech, plus a matching Simplified-Chinese translation. "
        "Keep it natural and brief; the translation should be natural Chinese, not word-for-word. Use the expression as it naturally occurs (its inflected form is fine). "
        'Format: {"english": ["A: ...", "B: ..."], "chinese": ["A：...", "B：..."]}. Return the two arrays with equal length.\n'
        "10. synonyms: 0 to 3 English words with the SAME meaning as the primary sense, each with a concise Chinese meaning; return [] if none fits well. Do not mix in synonyms of a different sense.\n"
        "11. similarPhrases: 0 to 3 short English phrases with a similar meaning to the primary sense, each with a concise Chinese meaning; return [] if none fits.\n"
        "12. Prefer meanings modern native speakers actually use; treat the Chinese meaning hint (if any) as weak guidance only.\n"
        "13. Return only this JSON shape: "
        '{"meaning": "...", "expressionType": "...", "alternativeMeanings": [{"meaning": "...", "type": "...", "note": "..."}], '
        '"usageScenario": "...", "exampleSentence": "...", "exampleTranslation": "...", '
        '"dialogue": {"english": ["A: ...", "B: ..."], "chinese": ["A：...", "B：..."]}, '
        '"synonyms": [{"english": "...", "chinese": "..."}], '
        '"similarPhrases": [{"english": "...", "chinese": "..."}]}'
        f"{stricter}"
    )
    return system_prompt, user_prompt


def _build_payload(
    text: str,
    chinese_meaning: str | None,
    *,
    strict_retry: bool,
    format_spec: dict[str, Any] | str,
    stream: bool = False,
) -> dict[str, Any]:
    system_prompt, user_prompt = _build_prompts(text, chinese_meaning, strict_retry=strict_retry)
    return {
        "model": settings.ollama_model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": stream,
        "think": settings.ollama_think,
        "format": format_spec,
        "options": {
            "temperature": settings.ollama_temperature,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
        "keep_alive": "30m",
    }


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _OllamaDeadlineExpired
    return remaining


def _post_generate(payload: dict[str, Any], deadline: float) -> requests.Response:
    with observed_operation(
        "ollama",
        "http_generate",
        attributes={"model": payload.get("model"), "stream": bool(payload.get("stream"))},
    ):
        return requests.post(
            _ollama_url(),
            json=payload,
            timeout=_remaining_timeout(deadline),
        )


def _call_once(
    text: str,
    chinese_meaning: str | None,
    *,
    deadline: float,
    strict_retry: bool = False,
    attempt_recorder: Callable[[], None] | None = None,
) -> _AttemptResult:
    format_spec: dict[str, Any] | str = _json_schema_format()
    payload = _build_payload(
        text,
        chinese_meaning,
        strict_retry=strict_retry,
        format_spec=format_spec,
    )

    try:
        if attempt_recorder:
            attempt_recorder()
        response = _post_generate(payload, deadline)
        if response.status_code == 400:
            fallback_payload = dict(payload)
            fallback_payload["format"] = "json"
            response = _post_generate(fallback_payload, deadline)
    except _OllamaDeadlineExpired:
        logger.warning("[ollama][diag] fail_reason=ollama_total_timeout")
        return _AttemptResult(None, None, "ollama_total_timeout", False)
    except requests.exceptions.Timeout:
        logger.warning("[ollama][diag] fail_reason=ollama_timeout")
        return _AttemptResult(None, None, "ollama_timeout", False)
    except requests.exceptions.RequestException:
        logger.warning("[ollama][diag] fail_reason=ollama_unavailable")
        return _AttemptResult(None, None, "ollama_unavailable", False)

    if response.status_code != 200:
        logger.warning(
            "[ollama][diag] fail_reason=ollama_http_error | status=%s | body=%r",
            response.status_code,
            response.text[:200],
        )
        return _AttemptResult(None, None, "ollama_http_error", False)

    try:
        body = response.json()
    except ValueError:
        logger.warning("[ollama][diag] fail_reason=json_parse_failed | invalid response body")
        return _AttemptResult(None, None, "json_parse_failed", True)

    content = str(body.get("response") or "").strip()
    if not content:
        logger.warning("[ollama][diag] fail_reason=empty_response")
        return _AttemptResult(None, None, "empty_response", False)

    try:
        data = json.loads(_strip_outer_code_fence(content))
    except json.JSONDecodeError as exc:
        logger.warning("[ollama][diag] fail_reason=json_parse_failed | %s | content=%r", exc, content[:200])
        return _AttemptResult(None, None, "json_parse_failed", True)

    return _normalize_analysis_data(text, data)


def generate_example_with_ollama(
    text: str,
    chinese_meaning: str | None = None,
    *,
    deadline: float | None = None,
    attempt_recorder: Callable[[], None] | None = None,
) -> tuple[str | None, str | None]:
    target = str(text or "").strip()
    if not target:
        return None, None

    if deadline is None:
        deadline = time.monotonic() + settings.ollama_timeout_seconds
    first = _call_once(
        target,
        chinese_meaning,
        deadline=deadline,
        strict_retry=False,
        attempt_recorder=attempt_recorder,
    )
    result = first
    if first.sentence is None and first.retryable and time.monotonic() < deadline:
        logger.info("[ollama][diag] retry | reason=%s", first.fail_reason)
        result = _call_once(
            target,
            chinese_meaning,
            deadline=deadline,
            strict_retry=True,
            attempt_recorder=attempt_recorder,
        )

    if result.sentence is None:
        return None, None

    if _is_valid_chinese_translation(result.translation):
        return result.sentence, result.translation

    if result.translation:
        logger.warning("[ollama][diag] fail_reason=qwen_example_translation_no_chinese")
    else:
        logger.warning("[ollama][diag] fail_reason=qwen_example_translation_missing")

    fallback_translation = ArgosTranslator().translate_to_zh(result.sentence)
    if not fallback_translation:
        logger.warning("[ollama][diag] fail_reason=argos_example_translation_failed")
    return result.sentence, fallback_translation


def generate_analysis_with_ollama(
    text: str,
    category: str | None = None,
    *,
    deadline: float | None = None,
    attempt_recorder: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """Generate meaning + expression type + alternative meanings + example + translation
    + synonyms + similarPhrases in one Ollama call.

    Returns None when Ollama cannot produce a usable example sentence. Otherwise returns a
    dict where ``meaning`` may be None (caller falls back to Argos for translation),
    ``expressionType`` is one of the EXPRESSION_TYPES enum, ``alternativeMeanings`` is a
    list of ``{meaning, type, note}`` (0..2 items), and ``synonyms`` / ``similarPhrases``
    are lists of ``{english, chinese}`` (possibly empty).

    ``deadline`` (a monotonic timestamp) bounds the total budget shared by the initial
    attempt and the single strict retry. When omitted, ``ollama_timeout_seconds`` from
    now is used. ``attempt_recorder`` is invoked once per actual model request (including
    the strict retry) so the caller can observe ``generation_attempt``.
    """
    target = str(text or "").strip()
    if not target:
        return None

    if deadline is None:
        deadline = time.monotonic() + settings.ollama_timeout_seconds
    first = _call_once(
        target,
        None,
        deadline=deadline,
        strict_retry=False,
        attempt_recorder=attempt_recorder,
    )
    result = first
    if first.sentence is None and first.retryable and time.monotonic() < deadline:
        logger.info("[ollama][diag] retry | reason=%s", first.fail_reason)
        result = _call_once(
            target,
            None,
            deadline=deadline,
            strict_retry=True,
            attempt_recorder=attempt_recorder,
        )

    if result.sentence is None:
        return None
    return _assemble_analysis_result(text, result)


_PAIR_RE = re.compile(r'^"([^"]*)"\s*:\s*(.+)$', re.DOTALL)


def _parse_pair(pair: str) -> tuple[str, Any] | None:
    """Parse one complete ``"key": value`` slice into ``(key, parsed_value)``."""
    text = pair.strip()
    if not text:
        return None
    match = _PAIR_RE.match(text)
    if not match:
        return None
    raw_value = match.group(2).strip()
    try:
        value = json.loads(raw_value)
    except ValueError:
        return None
    return match.group(1), value


def _extract_complete_fields(text: str) -> list[tuple[str, Any]]:
    """Extract every complete top-level field from a partial flat JSON object.

    Reprocessing the whole buffer each chunk is cheap (the payload is a few KB),
    and keeps the scanner trivial. Returns ``(key, value)`` pairs in the order
    they complete.
    """
    fields: list[tuple[str, Any]] = []
    n = len(text)
    start = 0
    while start < n and text[start] in " \t\r\n":
        start += 1
    if start >= n or text[start] != "{":
        return fields

    i = start + 1
    depth = 0  # nested { / [ inside the root object
    in_string = False
    escape = False
    field_start = start + 1

    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "{" or ch == "[":
            depth += 1
            i += 1
            continue
        if ch == "]":
            depth -= 1
            i += 1
            continue
        if ch == "}":
            if depth == 0:
                parsed = _parse_pair(text[field_start:i])
                if parsed is not None:
                    fields.append(parsed)
                return fields
            depth -= 1
            i += 1
            continue
        if ch == "," and depth == 0:
            parsed = _parse_pair(text[field_start:i])
            if parsed is not None:
                fields.append(parsed)
            field_start = i + 1
        i += 1

    return fields


def _post_generate_stream(payload: dict[str, Any], deadline: float) -> requests.Response:
    with observed_operation(
        "ollama",
        "http_generate_stream_open",
        attributes={"model": payload.get("model"), "stream": True},
    ):
        return requests.post(
            _ollama_url(),
            json=payload,
            stream=True,
            timeout=_remaining_timeout(deadline),
        )


_STREAM_FIELD_ORDER = [
    "meaning",
    "expressionType",
    "alternativeMeanings",
    "usageScenario",
    "exampleSentence",
    "exampleTranslation",
    "dialogue",
    "synonyms",
    "similarPhrases",
]


def generate_analysis_with_ollama_stream(
    text: str,
    category: str | None = None,
    *,
    deadline: float | None = None,
    attempt_recorder: Callable[[], None] | None = None,
    cancel_controller: StreamCancelController | None = None,
):
    """Stream a single Ollama analysis call, yielding field events as they complete.

    Yields ``("field", key, raw_value)`` for each complete top-level JSON field,
    then a final ``("result", dict | None)`` where the dict matches
    ``generate_analysis_with_ollama`` (or None on failure).

    The model does not necessarily emit fields in the prompt's order (constrained
    decoding tends to emit the schema ``required`` fields first). To guarantee the
    "primary meaning first" progressive UX, completed fields are buffered and
    re-emitted in ``_STREAM_FIELD_ORDER`` as soon as every earlier field is done.

    The single Ollama request is never retried here: a retry would mean a second
    model call, which the streaming flow must not make. ``deadline`` (a monotonic
    timestamp) bounds the single request; when omitted ``ollama_timeout_seconds``
    from now is used.

    ``cancel_controller`` (a :class:`StreamCancelController`) is polled on every
    streamed line and wires ``response.close()`` to the controller so a client
    disconnect can abort a blocking read from another thread. When the controller
    is firing, the generator yields ``("cancelled", None)`` instead of a result so
    the caller can classify the outcome as a cancellation, never a 500.
    """
    target = str(text or "").strip()
    if not target:
        yield ("result", None)
        return

    if deadline is None:
        deadline = time.monotonic() + settings.ollama_timeout_seconds
    payload = _build_payload(
        target,
        None,
        strict_retry=False,
        format_spec=_json_schema_format(),
        stream=True,
    )

    if cancel_controller is not None and cancel_controller.cancelled():
        logger.info("[ollama][diag] fail_reason=client_cancelled (pre-open)")
        yield ("cancelled", None)
        return

    try:
        if attempt_recorder:
            attempt_recorder()
        response = _post_generate_stream(payload, deadline)
    except _OllamaDeadlineExpired:
        logger.warning("[ollama][diag] fail_reason=ollama_total_timeout")
        yield ("result", None)
        return
    except requests.exceptions.Timeout:
        logger.warning("[ollama][diag] fail_reason=ollama_timeout")
        yield ("result", None)
        return
    except requests.exceptions.RequestException:
        logger.warning("[ollama][diag] fail_reason=ollama_unavailable")
        yield ("result", None)
        return

    if response.status_code != 200:
        logger.warning(
            "[ollama][diag] fail_reason=ollama_http_error | status=%s",
            response.status_code,
        )
        response.close()
        yield ("result", None)
        return

    if cancel_controller is not None:
        # Closing the response is what unblocks a ``iter_lines()`` read that is
        # stuck on the socket; registered once so a disconnect can abort it from
        # another thread instead of waiting for a socket timeout.
        cancel_controller.add_close_callback(response.close)

    buffer = ""
    completed: dict[str, Any] = {}
    emitted: set[str] = set()
    stream_result = "success"
    try:
        for raw_line in response.iter_lines(decode_unicode=False):
            if cancel_controller is not None and cancel_controller.cancelled():
                stream_result = "cancelled"
                break
            if time.monotonic() > deadline:
                raise _OllamaDeadlineExpired
            if not raw_line:
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            delta = obj.get("response")
            if isinstance(delta, str):
                buffer += delta
            for key, value in _extract_complete_fields(buffer):
                if key not in completed:
                    completed[key] = value
            # Emit completed fields in the fixed progressive order so the primary
            # meaning always appears first, regardless of the model's output order.
            # ``break`` (not ``continue``) holds later-priority fields back until
            # every earlier field has been emitted.
            for key in _STREAM_FIELD_ORDER:
                if key in emitted:
                    continue
                if key not in completed:
                    break
                emitted.add(key)
                yield ("field", key, completed[key])
            if obj.get("done"):
                break
    except GeneratorExit:
        stream_result = "cancelled"
        if cancel_controller is None or not cancel_controller.cancelled():
            logger.warning("[ollama][diag] fail_reason=client_cancelled")
        raise
    except _OllamaDeadlineExpired:
        stream_result = "timeout"
        logger.warning("[ollama][diag] fail_reason=ollama_total_timeout")
        yield ("result", None)
        return
    except requests.exceptions.Timeout:
        stream_result = "timeout"
        logger.warning("[ollama][diag] fail_reason=ollama_timeout")
        yield ("result", None)
        return
    except requests.exceptions.RequestException:
        stream_result = "error"
        logger.warning("[ollama][diag] fail_reason=ollama_unavailable")
        yield ("result", None)
        return
    except Exception:
        # The response was closed from another thread (a disconnect cancels the
        # bridge), so the mid-read call failed. Treat that as cancellation, not
        # as a 500; anything else is a genuine stream error.
        if cancel_controller is not None and cancel_controller.cancelled():
            stream_result = "cancelled"
        else:
            stream_result = "error"
            logger.warning("[ollama][diag] fail_reason=stream_read_error", exc_info=True)
            yield ("result", None)
            return
    finally:
        logger.info("[ollama][diag] stream_closed | result=%s", stream_result)
        response.close()

    if stream_result == "cancelled":
        yield ("cancelled", None)
        return

    content = buffer.strip()
    if not content:
        yield ("result", None)
        return
    try:
        data = json.loads(_strip_outer_code_fence(content))
    except json.JSONDecodeError as exc:
        logger.warning("[ollama][diag] fail_reason=json_parse_failed | %s | content=%r", exc, content[:200])
        yield ("result", None)
        return

    result = _normalize_analysis_data(target, data)
    if result.sentence is None:
        yield ("result", None)
        return
    yield ("result", _assemble_analysis_result(target, result))
