import unittest
from unittest.mock import patch


class AnalyzerLocalAiIntegrationTest(unittest.TestCase):
    def _run(
        self,
        text,
        translation_result=None,
        example_result=("She craves quiet mornings.", "她渴望安静的早晨。"),
        cached=None,
    ):
        from app.services.analyzer import analyze_text

        if translation_result is None:
            translation_result = {"ok": True, "translation": "基础翻译", "provider": "argos"}

        with (
            patch("app.services.analyzer.get_cache", return_value=cached),
            patch("app.services.analyzer.delete_cache") as delete_cache,
            patch("app.services.analyzer.set_cache") as set_cache,
            patch("app.services.analyzer.translate_to_zh", return_value=translation_result) as translate,
            patch("app.services.analyzer.generate_understanding", return_value="mock understanding"),
            patch("app.services.analyzer.generate_example_with_ollama", return_value=example_result) as ollama,
            patch("app.services.analyzer.generate_example_with_hunyuan") as hunyuan,
            patch("app.services.analyzer._generate_example_with_tmt") as tmt,
        ):
            result = analyze_text(text)
            return result, translate, ollama, hunyuan, tmt, set_cache, delete_cache

    def test_word_uses_argos_translation_and_qwen_example(self):
        result, translate, ollama, hunyuan, tmt, set_cache, _ = self._run("crave")
        self.assertTrue(result["ok"])
        self.assertEqual(result["category"], "word")
        self.assertEqual(result["translation"], "基础翻译")
        self.assertEqual(result["provider"], "argos")
        self.assertEqual(result["exampleSentence"], "She craves quiet mornings.")
        self.assertEqual(result["exampleTranslation"], "她渴望安静的早晨。")
        translate.assert_called_once_with("crave")
        ollama.assert_called_once_with("crave", "基础翻译")
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        set_cache.assert_called_once()

    def test_phrase_uses_contiguous_phrase_example(self):
        result, _, ollama, _, _, _, _ = self._run(
            "break a leg",
            example_result=("Break a leg at your audition tonight.", "祝你今晚试镜顺利。"),
        )
        self.assertEqual(result["category"], "phrase")
        self.assertEqual(result["exampleSentence"], "Break a leg at your audition tonight.")
        ollama.assert_called_once_with("break a leg", "基础翻译")

    def test_sentence_uses_argos_but_does_not_generate_extra_example(self):
        result, translate, ollama, hunyuan, tmt, set_cache, _ = self._run(
            "I study English every day."
        )
        self.assertEqual(result["category"], "sentence")
        self.assertEqual(result["translation"], "基础翻译")
        self.assertIsNone(result["exampleSentence"])
        ollama.assert_not_called()
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        translate.assert_called_once()
        set_cache.assert_called_once()

    def test_ollama_unavailable_keeps_translation_and_does_not_cache_empty_word_example(self):
        result, _, ollama, _, tmt, set_cache, _ = self._run("crave", example_result=(None, None))
        self.assertTrue(result["ok"])
        self.assertEqual(result["translation"], "基础翻译")
        self.assertIsNone(result["exampleSentence"])
        ollama.assert_called_once()
        tmt.assert_not_called()
        set_cache.assert_not_called()

    def test_argos_unavailable_still_attempts_qwen_example(self):
        result, _, ollama, _, _, set_cache, _ = self._run(
            "crave",
            translation_result={"ok": False, "translation": None, "provider": "argos"},
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["translation"])
        self.assertEqual(result["exampleSentence"], "She craves quiet mornings.")
        ollama.assert_called_once_with("crave", None)
        set_cache.assert_not_called()

    def test_both_local_ai_dependencies_unavailable_do_not_500(self):
        result, _, ollama, hunyuan, tmt, set_cache, _ = self._run(
            "crave",
            translation_result={"ok": False, "translation": None, "provider": "argos"},
            example_result=(None, None),
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["translation"])
        self.assertIsNone(result["exampleSentence"])
        self.assertTrue(result["warnings"])
        ollama.assert_called_once()
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        set_cache.assert_not_called()

    def test_stale_empty_word_example_cache_is_ignored(self):
        cached = {
            "ok": True,
            "level": "pass",
            "category": "word",
            "normalizedText": "crave",
            "translation": "旧翻译",
            "provider": "argos",
            "exampleSentence": None,
            "exampleTranslation": None,
        }
        result, translate, ollama, _, _, _, delete_cache = self._run("crave", cached=cached)
        self.assertEqual(result["translation"], "基础翻译")
        translate.assert_called_once()
        ollama.assert_called_once()
        delete_cache.assert_called_once()

    def test_default_flow_never_calls_tencent_or_hunyuan(self):
        _, _, _, hunyuan, tmt, _, _ = self._run("well-known")
        hunyuan.assert_not_called()
        tmt.assert_not_called()


class AnalyzerErrorHandlingTest(unittest.TestCase):
    """Fix 4: analyzer must not return internal exception strings to frontend."""

    def test_internal_exception_not_leaked_to_response(self):
        """Internal exception details (paths, secrets) must NOT appear in API response."""
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  side_effect=RuntimeError("C:\\secret\\path database_password=abc")),
            patch("app.services.analyzer.generate_understanding"),
            patch("app.services.analyzer.generate_example_with_ollama"),
        ):
            result = analyze_text("crave")

        self.assertFalse(result["ok"])
        self.assertEqual(result["level"], "failed")
        self.assertEqual(result["category"], "unknown")

        errors = result["errors"]
        self.assertEqual(len(errors), 1)
        # Must be the fixed user-facing message, not the raw exception
        self.assertEqual(errors[0], "分析服务暂时不可用，请稍后重试")

        # Verify no internal details leaked
        error_text = " ".join(errors)
        self.assertNotIn("secret", error_text)
        self.assertNotIn("path", error_text)
        self.assertNotIn("database_password", error_text)
        self.assertNotIn("RuntimeError", error_text)

    def test_logger_exception_called_on_unexpected_failure(self):
        """logger.exception must be called to preserve full traceback in logs."""
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  side_effect=RuntimeError("internal error")),
            patch("app.services.analyzer.generate_understanding"),
            patch("app.services.analyzer.generate_example_with_ollama"),
            patch("app.services.analyzer.logger") as mock_logger,
        ):
            result = analyze_text("crave")

        self.assertFalse(result["ok"])
        mock_logger.exception.assert_called_once()
        call_arg = mock_logger.exception.call_args[0][0]
        self.assertIn("[analyzer] unexpected failure", call_arg)


if __name__ == "__main__":
    unittest.main()
