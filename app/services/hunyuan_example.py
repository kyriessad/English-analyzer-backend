"""
Generate English example sentences via TokenHub Hunyuan (OpenAI-compatible API).
Failure is always silent and never affects the main analysis flow.
"""
import json
import logging

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


def generate_example_with_hunyuan(
    text: str,
    chinese_meaning: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Call TokenHub Hunyuan ChatCompletions to generate an example sentence.

    Returns (exampleSentence, exampleTranslation) or (None, None) on any failure,
    including missing API key, network error, or validation mismatch.
    """
    try:
        api_key = (settings.hunyuan_api_key or "").strip()
        if not api_key:
            logger.info("[hunyuan] HUNYUAN_API_KEY not configured, skipping")
            return None, None

        base_url = settings.hunyuan_base_url
        model = settings.hunyuan_model

        logger.info(
            "[hunyuan] API key configured: True | base_url: %s | model: %s",
            base_url, model,
        )

        meaning_hint = f"（含义：{chinese_meaning}）" if chinese_meaning else ""

        system_prompt = (
            "You are an English teacher helping Chinese learners. "
            "Generate a natural English example sentence and its Chinese translation. "
            "You must ONLY return valid JSON, no markdown, no explanation."
        )

        user_prompt = (
            f'Create an example sentence for the English word/phrase "{text}"{meaning_hint}.\n'
            "Requirements:\n"
            f'1. exampleSentence must be a natural, complete English sentence that contains "{text}" or its inflection\n'
            "2. The example should be concise, daily-life, suitable for English learners\n"
            "3. exampleTranslation must be the natural Chinese translation of the example sentence\n"
            "4. Return ONLY a JSON object (no markdown, no extra text):\n"
            '{"exampleSentence": "...", "exampleTranslation": "..."}'
        )

        url = f"{base_url}/chat/completions"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=15,
        )

        if resp.status_code != 200:
            error_hint = ""
            try:
                error_body = resp.json()
                error_hint = error_body.get("error", {}).get("message", "") or str(error_body)[:200]
            except Exception:
                error_hint = resp.text[:200]
            logger.warning(
                "[hunyuan] HTTP %s from %s: %s",
                resp.status_code, base_url, error_hint,
            )
            return None, None

        body = resp.json()
        choices = body.get("choices", [])
        if not choices:
            logger.warning("[hunyuan] response has no choices")
            return None, None

        content = str(choices[0].get("message", {}).get("content", "")).strip()
        if not content:
            logger.warning("[hunyuan] response content is empty")
            return None, None

        logger.info("[hunyuan] content length: %d", len(content))

        # Extract JSON — guard against markdown wrapping
        start = content.find("{")
        end = content.rfind("}") + 1
        if start == -1 or end <= start:
            logger.warning("[hunyuan] no JSON object found in content: %s", content[:120])
            return None, None

        try:
            data = json.loads(content[start:end])
        except json.JSONDecodeError as e:
            logger.warning("[hunyuan] JSON parse failed: %s | raw: %s", e, content[:200])
            return None, None

        sentence = str(data.get("exampleSentence") or "").strip()
        translation = str(data.get("exampleTranslation") or "").strip()

        # Both fields must be present
        if not sentence or not translation:
            logger.warning("[hunyuan] validation FAIL: sentence or translation empty")
            return None, None
        # Must not be the bare input text itself
        if sentence.lower().strip() == text.lower().strip():
            logger.warning("[hunyuan] validation FAIL: sentence equals input text")
            return None, None
        # Must contain the original input (substring, case-insensitive)
        if text.lower() not in sentence.lower():
            logger.warning(
                "[hunyuan] validation FAIL: sentence does not contain '%s': %s",
                text, sentence[:120],
            )
            return None, None
        # Must be a real sentence, not a single word
        if len(sentence.split()) < 3:
            logger.warning("[hunyuan] validation FAIL: too few words: %s", sentence)
            return None, None

        logger.info("[hunyuan] SUCCESS for '%s': %s", text, sentence[:80])
        return sentence, translation
    except Exception:
        logger.exception("[hunyuan] unexpected error for '%s'", text)
        return None, None
