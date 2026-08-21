import unittest
from unittest.mock import ANY, patch


_UNSET = object()


class AnalyzerLocalAiIntegrationTest(unittest.TestCase):
    def _run(
        self,
        text,
        translation_result=None,
        analysis_result=_UNSET,
        cached=None,
    ):
        from app.services.analyzer import analyze_text

        if translation_result is None:
            translation_result = {"ok": True, "translation": "基础翻译", "provider": "argos"}

        if analysis_result is _UNSET:
            analysis_result = {
                "meaning": None,
                "exampleSentence": "She craves quiet mornings.",
                "exampleTranslation": "她渴望安静的早晨。",
                "synonyms": [],
                "similarPhrases": [],
            }

        with (
            patch("app.services.analyzer.get_cache", return_value=cached),
            patch("app.services.analyzer.delete_cache") as delete_cache,
            patch("app.services.analyzer.set_cache") as set_cache,
            patch("app.services.analyzer.translate_to_zh", return_value=translation_result) as translate,
            patch("app.services.analyzer.get_dictionary_translation", return_value=None),
            patch("app.services.analyzer.generate_understanding", return_value="mock understanding"),
            patch("app.services.analyzer.generate_analysis_with_ollama", return_value=analysis_result) as ollama,
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
        self.assertEqual(result["synonyms"], [])
        self.assertEqual(result["similarPhrases"], [])
        translate.assert_called_once_with("crave")
        ollama.assert_called_once_with("crave", "word", deadline=ANY, attempt_recorder=None)
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        set_cache.assert_called_once()

    def test_phrase_uses_contiguous_phrase_example(self):
        result, _, ollama, _, _, _, _ = self._run(
            "break a leg",
            analysis_result={
                "meaning": None,
                "exampleSentence": "Break a leg at your audition tonight.",
                "exampleTranslation": "祝你今晚试镜顺利。",
                "synonyms": [],
                "similarPhrases": [],
            },
        )
        self.assertEqual(result["category"], "phrase")
        self.assertEqual(result["exampleSentence"], "Break a leg at your audition tonight.")
        ollama.assert_called_once_with("break a leg", "phrase", deadline=ANY, attempt_recorder=None)

    def test_word_ollama_meaning_synonyms_and_phrases_propagate(self):
        result, translate, ollama, _, _, _, _ = self._run(
            "crave",
            analysis_result={
                "meaning": "渴望",
                "exampleSentence": "She craves quiet mornings.",
                "exampleTranslation": "她渴望安静的早晨。",
                "synonyms": [{"english": "long for", "chinese": "渴望"}],
                "similarPhrases": [{"english": "hunger for", "chinese": "渴望"}],
            },
        )
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["translation"], "渴望")
        self.assertEqual(result["exampleSentence"], "She craves quiet mornings.")
        self.assertEqual(
            result["synonyms"],
            [{"english": "long for", "chinese": "渴望"}],
        )
        self.assertEqual(
            result["similarPhrases"],
            [{"english": "hunger for", "chinese": "渴望"}],
        )
        translate.assert_not_called()

    def test_expression_type_and_alternatives_propagate(self):
        result, _, ollama, _, _, _, _ = self._run(
            "break a leg",
            analysis_result={
                "meaning": "祝你好运",
                "expressionType": "idiom",
                "alternativeMeanings": [
                    {"meaning": "摔断腿", "type": "literal", "note": "字面意思"},
                ],
                "exampleSentence": "Break a leg at your audition tonight.",
                "exampleTranslation": "祝你今晚试镜顺利。",
                "synonyms": [],
                "similarPhrases": [],
            },
        )
        self.assertEqual(result["expressionType"], "idiom")
        self.assertEqual(
            result["alternativeMeanings"],
            [{"meaning": "摔断腿", "type": "literal", "note": "字面意思"}],
        )
        ollama.assert_called_once_with("break a leg", "phrase", deadline=ANY, attempt_recorder=None)

    def test_fallback_expression_fields_default_to_literal_empty(self):
        result, _, ollama, _, tmt, set_cache, _ = self._run("crave", analysis_result=None)
        self.assertEqual(result["expressionType"], "literal")
        self.assertEqual(result["alternativeMeanings"], [])

    def test_sentence_prefers_ollama_like_word_and_phrase(self):
        result, translate, ollama, hunyuan, tmt, set_cache, _ = self._run(
            "I study English every day."
        )
        self.assertEqual(result["category"], "sentence")
        self.assertEqual(result["translation"], "基础翻译")
        self.assertEqual(result["exampleSentence"], "She craves quiet mornings.")
        ollama.assert_called_once_with("I study English every day.", "sentence", deadline=ANY, attempt_recorder=None)
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        translate.assert_called_once()
        set_cache.assert_called_once()

    def test_sentence_analysis_source_reports_ollama(self):
        result, _, _, _, _, _, _ = self._run(
            "I'm down.",
            analysis_result={
                "meaning": "我愿意 / 我可以",
                "expressionType": "colloquial",
                "alternativeMeanings": [],
                "usageScenario": "朋友邀请你一起参加活动时。",
                "exampleSentence": "I'm down for dinner tonight.",
                "exampleTranslation": "今晚一起吃饭的话，我可以。",
                "dialogue": {
                    "english": ["A: Want to grab some food?", "B: Yeah, I'm down."],
                    "chinese": ["A：要不要吃点东西？", "B：可以啊。"],
                },
                "synonyms": [{"english": "on board", "chinese": "愿意"}],
                "similarPhrases": [{"english": "I'm in.", "chinese": "算我一个"}],
            },
        )
        self.assertEqual(result["category"], "sentence")
        self.assertEqual(result["analysisSource"], "ollama")
        self.assertEqual(result["analysisModel"], "qwen3:8b")
        self.assertEqual(result["usageScenario"], "朋友邀请你一起参加活动时。")
        self.assertEqual(
            result["dialogue"],
            {
                "english": ["A: Want to grab some food?", "B: Yeah, I'm down."],
                "chinese": ["A：要不要吃点东西？", "B：可以啊。"],
            },
        )

    def test_ollama_failure_reports_clear_failure_not_fallback(self):
        result, _, ollama, hunyuan, tmt, set_cache, _ = self._run("I'm down.", analysis_result=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["level"], "failed")
        self.assertIsNone(result["analysisSource"])
        self.assertIsNone(result["analysisModel"])
        self.assertEqual(result["usageScenario"], "")
        self.assertEqual(result["dialogue"], {"english": [], "chinese": []})
        self.assertEqual(result["errors"], ["分析服务暂时不可用，请稍后重试"])
        ollama.assert_called_once()
        hunyuan.assert_not_called()
        tmt.assert_not_called()
        set_cache.assert_not_called()

    def test_ollama_unavailable_reports_clear_failure_not_local_template(self):
        result, _, ollama, _, tmt, set_cache, _ = self._run("crave", analysis_result=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["level"], "failed")
        self.assertIsNone(result["translation"])
        self.assertIsNone(result["exampleSentence"])
        self.assertNotEqual(result["exampleSource"], "local_template")
        self.assertEqual(result["synonyms"], [])
        self.assertEqual(result["similarPhrases"], [])
        self.assertEqual(result["errors"], ["分析服务暂时不可用，请稍后重试"])
        ollama.assert_called_once_with("crave", "word", deadline=ANY, attempt_recorder=None)
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
        ollama.assert_called_once_with("crave", "word", deadline=ANY, attempt_recorder=None)
        set_cache.assert_not_called()

    def test_dictionary_fallback_uses_the_sense_found_in_example_translation(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache") as set_cache,
            patch(
                "app.services.analyzer.translate_to_zh",
                return_value={"ok": False, "translation": None, "provider": "argos"},
            ),
            patch(
                "app.services.analyzer.generate_analysis_with_ollama",
                return_value={
                    "meaning": None,
                    "exampleSentence": "The doctor checked the organ.",
                    "exampleTranslation": "医生检查了这个器官。",
                    "synonyms": [],
                    "similarPhrases": [],
                },
            ),
            patch(
                "app.services.analyzer.get_dictionary_translation",
                return_value="器官",
            ) as dictionary,
        ):
            result = analyze_text("organ")

        self.assertEqual(result["translation"], "器官")
        self.assertEqual(result["provider"], "ecdict")
        self.assertNotIn("翻译暂时不可用，已先保存英文内容。", result["warnings"])
        dictionary.assert_called_once_with("organ", "医生检查了这个器官。")
        set_cache.assert_called_once()

    def test_contextual_dictionary_sense_overrides_an_ambiguous_machine_translation(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch(
                "app.services.analyzer.translate_to_zh",
                return_value={"ok": True, "translation": "机关", "provider": "argos"},
            ),
            patch(
                "app.services.analyzer.generate_analysis_with_ollama",
                return_value={
                    "meaning": None,
                    "exampleSentence": "The heart is an important organ.",
                    "exampleTranslation": "心脏是人体中一个重要的器官。",
                    "synonyms": [],
                    "similarPhrases": [],
                },
            ),
            patch(
                "app.services.analyzer.get_dictionary_translation",
                return_value="器官",
            ),
        ):
            result = analyze_text("organ")

        self.assertEqual(result["translation"], "器官")
        self.assertEqual(result["provider"], "ecdict")

    def test_both_local_ai_dependencies_unavailable_do_not_500(self):
        result, _, ollama, hunyuan, tmt, set_cache, _ = self._run(
            "crave",
            translation_result={"ok": False, "translation": None, "provider": "argos"},
            analysis_result=None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["level"], "failed")
        self.assertIsNone(result["translation"])
        self.assertIsNone(result["exampleSentence"])
        self.assertEqual(result["errors"], ["分析服务暂时不可用，请稍后重试"])
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

    def test_force_refresh_skips_cache_and_recomputes(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache") as get_cache,
            patch("app.services.analyzer.set_cache") as set_cache,
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "基础翻译", "provider": "argos"}),
            patch("app.services.analyzer.get_dictionary_translation", return_value=None),
            patch("app.services.analyzer.generate_understanding", return_value="mock understanding"),
            patch("app.services.analyzer.generate_analysis_with_ollama",
                  return_value={
                      "meaning": "渴望",
                      "exampleSentence": "She craves quiet mornings.",
                      "exampleTranslation": "她渴望安静的早晨。",
                      "synonyms": [],
                      "similarPhrases": [],
                  }) as ollama,
        ):
            result = analyze_text("crave", force_refresh=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["translation"], "渴望")
        get_cache.assert_not_called()
        ollama.assert_called_once_with("crave", "word", deadline=ANY, attempt_recorder=None)
        set_cache.assert_called_once()


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
            patch(
                "app.services.analyzer.generate_analysis_with_ollama",
                return_value={
                    "meaning": None,
                    "exampleSentence": "She craves quiet mornings.",
                    "exampleTranslation": "她渴望安静的早晨。",
                    "synonyms": [],
                    "similarPhrases": [],
                },
            ),
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
            patch(
                "app.services.analyzer.generate_analysis_with_ollama",
                return_value={
                    "meaning": None,
                    "exampleSentence": "She craves quiet mornings.",
                    "exampleTranslation": "她渴望安静的早晨。",
                    "synonyms": [],
                    "similarPhrases": [],
                },
            ),
            patch("app.services.analyzer.logger") as mock_logger,
        ):
            result = analyze_text("crave")

        self.assertFalse(result["ok"])
        mock_logger.exception.assert_called_once()
        call_arg = mock_logger.exception.call_args[0][0]
        self.assertIn("[analyzer] unexpected failure", call_arg)


if __name__ == "__main__":
    unittest.main()
