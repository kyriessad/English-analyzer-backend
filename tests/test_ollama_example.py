import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def ollama_body(sentence, translation="中文翻译"):
    return FakeResponse(
        200,
        {"response": json.dumps({
            "exampleSentence": sentence,
            "exampleTranslation": translation,
        })},
    )


class OllamaExampleTest(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="qwen3:8b",
            ollama_timeout_seconds=60,
            ollama_temperature=0.3,
            ollama_think=False,
        )
        self.settings_patch = patch("app.services.ollama_example.settings", self.settings)
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()

    def test_normal_json_schema_response(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", return_value=ollama_body("She craves quiet mornings.")):
            self.assertEqual(
                generate_example_with_ollama("crave", "渴望"),
                ("She craves quiet mornings.", "中文翻译"),
            )

    def test_request_parameters(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", return_value=ollama_body("She craves quiet mornings.")) as post:
            generate_example_with_ollama("crave")

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen3:8b")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["temperature"], 0.3)
        self.assertEqual(payload["options"]["num_predict"], 512)
        self.assertIsInstance(payload["format"], dict)
        timeout_val = post.call_args.kwargs["timeout"]
        self.assertGreater(timeout_val, 0)
        self.assertLessEqual(timeout_val, self.settings.ollama_timeout_seconds)

    def test_connection_refused(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", side_effect=requests.exceptions.ConnectionError):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_timeout(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", side_effect=requests.exceptions.Timeout):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_non_200(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", return_value=FakeResponse(500, text="error")):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_empty_response_field(self):
        from app.services.ollama_example import generate_example_with_ollama

        with patch("app.services.ollama_example.requests.post", return_value=FakeResponse(200, {"response": ""})):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_json_parse_failed(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = FakeResponse(200, {"response": "not json"})
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]) as post:
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))
            self.assertEqual(post.call_count, 2)

    def test_missing_example_sentence(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = FakeResponse(200, {"response": json.dumps({"exampleTranslation": "x"})})
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_empty_chinese_translation_uses_argos(self):
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post", return_value=ollama_body("She craves quiet mornings.", "")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh", return_value="她渴望安静的早晨。") as argos,
        ):
            self.assertEqual(
                generate_example_with_ollama("crave"),
                ("She craves quiet mornings.", "她渴望安静的早晨。"),
            )
            argos.assert_called_once_with("She craves quiet mornings.")

    def test_example_without_target_fails_validation(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = ollama_body("She wants quiet mornings.")
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]):
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))

    def test_split_phrase_fails_validation(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = ollama_body("Break the news gently before you move a leg.")
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]):
            self.assertEqual(generate_example_with_ollama("break a leg"), (None, None))

    def test_bare_target_with_punctuation_fails_validation(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = ollama_body("Break a leg!")
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]):
            self.assertEqual(generate_example_with_ollama("break a leg"), (None, None))

    def test_retry_once_then_success(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = ollama_body("She wants quiet mornings.")
        good = ollama_body("She craves quiet mornings.")
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, good]) as post:
            self.assertEqual(
                generate_example_with_ollama("crave"),
                ("She craves quiet mornings.", "中文翻译"),
            )
            self.assertEqual(post.call_count, 2)

    def test_retry_after_failure_still_fails(self):
        from app.services.ollama_example import generate_example_with_ollama

        bad = ollama_body("She wants quiet mornings.")
        with patch("app.services.ollama_example.requests.post", side_effect=[bad, bad]) as post:
            self.assertEqual(generate_example_with_ollama("crave"), (None, None))
            self.assertEqual(post.call_count, 2)


class OllamaDeadlineBudgetTest(unittest.TestCase):
    """Fix 1: total deadline budget shared across all requests."""

    def setUp(self):
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="qwen3:8b",
            ollama_timeout_seconds=60,
            ollama_temperature=0.3,
            ollama_think=False,
        )
        self.settings_patch = patch("app.services.ollama_example.settings", self.settings)
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()

    def test_total_deadline_created_once(self):
        """Deadline is created only once at the entry point."""
        from app.services.ollama_example import generate_example_with_ollama

        # Two attempts: first fails validation, retry succeeds
        bad = ollama_body("She wants quiet mornings.")
        good = ollama_body("She craves quiet mornings.")

        with patch("app.services.ollama_example.requests.post", side_effect=[bad, good]):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", "中文翻译"))

    def test_strict_retry_uses_smaller_timeout(self):
        """Second strict retry gets less remaining time than the first request."""
        from app.services.ollama_example import generate_example_with_ollama

        # Simulate time passing: T0=100 (deadline set), T1=100.1 (first req),
        # T2=130 (retry gate), T3=130.1 (retry req). Extra monotonic reads (e.g.
        # example validation) fall back to 999 so the mock never runs dry.
        timestamps = iter([100.0, 100.1, 130.0, 130.1])

        def fake_monotonic():
            return next(timestamps, 999.0)

        bad = ollama_body("She wants quiet mornings.")
        good = ollama_body("She craves quiet mornings.")
        captured_timeouts = []

        def fake_post(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            if len(captured_timeouts) == 1:
                return bad
            return good

        with patch("app.services.ollama_example.time.monotonic", side_effect=fake_monotonic):
            with patch("app.services.ollama_example.requests.post", side_effect=fake_post):
                result = generate_example_with_ollama("crave")

        self.assertEqual(result, ("She craves quiet mornings.", "中文翻译"))
        self.assertEqual(len(captured_timeouts), 2)
        # Deadline = 100.0 + 60 = 160.0
        # First timeout = 160.0 - 100.1 ≈ 59.9
        # Retry timeout = 160.0 - 130.0 = 30.0
        self.assertLess(captured_timeouts[1], captured_timeouts[0])
        # Retry has significantly less time (used ~30s of budget)
        self.assertLess(captured_timeouts[1], captured_timeouts[0] * 0.6)

    def test_schema_400_fallback_uses_shared_deadline(self):
        """Schema 400 fallback gets remaining timeout, not a fresh budget."""
        from app.services.ollama_example import _call_once

        timestamps = [100.0, 100.1, 120.0]

        responses = [
            FakeResponse(400, text="bad request"),
            ollama_body("She craves quiet mornings."),
        ]
        resp_iter = iter(responses)
        captured_timeouts = []

        def fake_post(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            return next(resp_iter)

        deadline = 160.0  # 100 + 60
        with patch("app.services.ollama_example.time.monotonic", side_effect=timestamps):
            with patch("app.services.ollama_example.requests.post", side_effect=fake_post):
                result = _call_once("crave", None, deadline=deadline)

        self.assertEqual(result.sentence, "She craves quiet mornings.")
        self.assertEqual(len(captured_timeouts), 2)
        # Second request gets less remaining time
        self.assertLess(captured_timeouts[1], captured_timeouts[0])

    def test_budget_exhausted_before_retry_no_request(self):
        """After first response, if budget exhausted, skip strict retry."""
        from app.services.ollama_example import generate_example_with_ollama

        # T0=100, T1=100.1 (first req ok), T2=170 (past deadline 160)
        timestamps = [100.0, 100.1, 170.0]

        bad = ollama_body("She wants quiet mornings.")  # triggers retry
        call_count = [0]

        def fake_post(*args, **kwargs):
            call_count[0] += 1
            return bad

        with patch("app.services.ollama_example.time.monotonic", side_effect=timestamps):
            with patch("app.services.ollama_example.requests.post", side_effect=fake_post):
                result = generate_example_with_ollama("crave")

        self.assertEqual(result, (None, None))
        # Only 1 HTTP request - no retry because budget exhausted
        self.assertEqual(call_count[0], 1)

    def test_deadline_exhausted_before_first_request_no_http(self):
        """When deadline already expired, no HTTP request is sent at all."""
        from app.services.ollama_example import generate_example_with_ollama

        # T0=100, T1=170 (past deadline 160), T2=170
        timestamps = [100.0, 170.0, 170.0]
        call_count = [0]

        def fake_post(*args, **kwargs):
            call_count[0] += 1
            return ollama_body("She craves quiet mornings.")

        with patch("app.services.ollama_example.time.monotonic", side_effect=timestamps):
            with patch("app.services.ollama_example.requests.post", side_effect=fake_post):
                result = generate_example_with_ollama("crave")

        self.assertEqual(result, (None, None))
        self.assertEqual(call_count[0], 0)

    def test_budget_exhausted_returns_none_none_no_exception(self):
        """Deadline exhaustion returns (None, None) without raising."""
        from app.services.ollama_example import generate_example_with_ollama

        timestamps = [100.0, 170.0, 170.0]

        with patch("app.services.ollama_example.time.monotonic", side_effect=timestamps):
            with patch("app.services.ollama_example.requests.post") as post:
                result = generate_example_with_ollama("crave")

        self.assertEqual(result, (None, None))
        post.assert_not_called()

    def test_deadline_exhausted_fail_reason_is_ollama_total_timeout(self):
        """Deadline exhausted produces 'ollama_total_timeout' fail reason."""
        from app.services.ollama_example import _call_once, _OllamaDeadlineExpired

        with patch("app.services.ollama_example.time.monotonic", return_value=200.0):
            result = _call_once("crave", None, deadline=150.0)

        self.assertIsNone(result.sentence)
        self.assertIsNone(result.translation)
        self.assertEqual(result.fail_reason, "ollama_total_timeout")
        self.assertFalse(result.retryable)


class OllamaChineseTranslationTest(unittest.TestCase):
    """Fix 2: validate Qwen Chinese translations."""

    def setUp(self):
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="qwen3:8b",
            ollama_timeout_seconds=60,
            ollama_temperature=0.3,
            ollama_think=False,
        )
        self.settings_patch = patch("app.services.ollama_example.settings", self.settings)
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()

    def test_valid_chinese_translation_no_argos(self):
        """Qwen returns valid Chinese → no Argos fallback."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves quiet mornings.", "她渴望安静的早晨。")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh") as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", "她渴望安静的早晨。"))
            argos.assert_not_called()

    def test_empty_translation_falls_back_to_argos(self):
        """Qwen returns empty string → Argos fallback."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves quiet mornings.", "")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh",
                  return_value="她渴望安静的早晨。") as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", "她渴望安静的早晨。"))
            argos.assert_called_once_with("She craves quiet mornings.")

    def test_pure_english_translation_falls_back_to_argos(self):
        """Qwen returns pure English as translation → Argos fallback."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves quiet mornings.", "This is a translation.")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh",
                  return_value="她渴望安静的早晨。") as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", "她渴望安静的早晨。"))
            argos.assert_called_once()

    def test_pure_numbers_translation_falls_back_to_argos(self):
        """Qwen returns pure numbers → Argos fallback."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves quiet mornings.", "12345")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh",
                  return_value="她渴望安静的早晨。") as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", "她渴望安静的早晨。"))
            argos.assert_called_once()

    def test_chinese_with_english_name_is_valid(self):
        """Chinese mixed with English proper names is considered valid."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves working at Google.", "她渴望在Google工作。")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh") as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves working at Google.", "她渴望在Google工作。"))
            argos.assert_not_called()

    def test_qwen_invalid_and_argos_fails_returns_english_only(self):
        """Qwen translation invalid + Argos fails → English example, None translation."""
        from app.services.ollama_example import generate_example_with_ollama

        with (
            patch("app.services.ollama_example.requests.post",
                  return_value=ollama_body("She craves quiet mornings.", "N/A")),
            patch("app.services.ollama_example.ArgosTranslator.translate_to_zh",
                  return_value=None) as argos,
        ):
            result = generate_example_with_ollama("crave")
            self.assertEqual(result, ("She craves quiet mornings.", None))
            argos.assert_called_once_with("She craves quiet mornings.")


class OllamaExpressionAnalysisTest(unittest.TestCase):
    """Expression analysis: expressionType + alternativeMeanings parsing."""

    def setUp(self):
        self.settings = SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="qwen3:8b",
            ollama_timeout_seconds=60,
            ollama_temperature=0.3,
            ollama_think=False,
        )
        self.settings_patch = patch("app.services.ollama_example.settings", self.settings)
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()

    def _body(self, extra):
        payload = {
            "meaning": "渴望",
            "exampleSentence": "She craves quiet mornings.",
            "exampleTranslation": "她渴望安静的早晨。",
        }
        payload.update(extra)
        return FakeResponse(200, {"response": json.dumps(payload)})

    def test_expression_type_and_alternatives_parsed(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({
            "expressionType": "idiom",
            "alternativeMeanings": [
                {"meaning": "祝你好运", "type": "literal", "note": "字面意思是摔断腿"},
            ],
        })
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(result["expressionType"], "idiom")
        self.assertEqual(len(result["alternativeMeanings"]), 1)
        self.assertEqual(result["alternativeMeanings"][0]["meaning"], "祝你好运")
        self.assertEqual(result["alternativeMeanings"][0]["type"], "literal")

    def test_alternatives_truncated_to_two(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({
            "alternativeMeanings": [
                {"meaning": "含义一"},
                {"meaning": "含义二"},
                {"meaning": "含义三"},
            ],
        })
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(len(result["alternativeMeanings"]), 2)

    def test_non_chinese_alternative_meaning_is_dropped(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({
            "alternativeMeanings": [
                {"meaning": "That's cold", "type": "literal", "note": "physical cold"},
                {"meaning": "太狠了", "type": "colloquial", "note": "形容无情"},
            ],
        })
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(len(result["alternativeMeanings"]), 1)
        self.assertEqual(result["alternativeMeanings"][0]["meaning"], "太狠了")

    def test_unknown_expression_type_normalized_to_literal(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({"expressionType": "weird_category"})
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(result["expressionType"], "literal")

    def test_missing_expression_fields_default_to_literal_empty(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({})  # old shape: no expressionType / alternativeMeanings
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(result["expressionType"], "literal")
        self.assertEqual(result["alternativeMeanings"], [])

    def test_old_fields_still_present(self):
        from app.services.ollama_example import generate_analysis_with_ollama

        body = self._body({
            "synonyms": [{"english": "long for", "chinese": "渴望"}],
            "similarPhrases": [{"english": "hunger for", "chinese": "渴望"}],
        })
        with patch("app.services.ollama_example.requests.post", return_value=body):
            result = generate_analysis_with_ollama("crave")

        self.assertEqual(result["meaning"], "渴望")
        self.assertEqual(result["exampleSentence"], "She craves quiet mornings.")
        self.assertEqual(result["synonyms"], [{"english": "long for", "chinese": "渴望"}])
        self.assertEqual(result["similarPhrases"], [{"english": "hunger for", "chinese": "渴望"}])


if __name__ == "__main__":
    unittest.main()
