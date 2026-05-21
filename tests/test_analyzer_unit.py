"""
Unit tests for app.services.analyzer.analyze_text.
Focus: example-sentence generation gate (Phase 8D-hotfix).
"""
import unittest
from unittest.mock import patch


class AnalyzerExampleGateTest(unittest.TestCase):
    """
    Verify that word/phrase inputs attempt example generation even when
    translation is unavailable (Phase 8D-hotfix: removed 'and translation' gate).
    """

    def _call(self, text, translation_ok=True, translation_value="测试中文"):
        """Run analyze_text with controlled translator and Hunyuan mocks."""
        from app.services.analyzer import analyze_text

        translation_result = (
            {"ok": True, "translation": translation_value, "provider": "tencent"}
            if translation_ok
            else {"ok": False, "translation": None, "provider": None}
        )

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh", return_value=translation_result),
            patch("app.services.analyzer.generate_understanding", return_value="mocked understanding"),
            patch("app.services.analyzer.generate_example_with_hunyuan") as mock_hunyuan,
            patch("app.services.analyzer._generate_example_with_tmt") as mock_tmt,
        ):
            mock_hunyuan.return_value = (None, None)
            mock_tmt.return_value = (None, None)
            result = analyze_text(text)
            return result, mock_hunyuan, mock_tmt

    # ------------------------------------------------------------------
    # Word + translation available
    # ------------------------------------------------------------------

    def test_word_with_translation_calls_hunyuan(self):
        result, mock_hunyuan, mock_tmt = self._call("apply", translation_ok=True)
        self.assertEqual(result["category"], "word")
        mock_hunyuan.assert_called_once_with("apply", "测试中文")

    def test_word_with_translation_calls_tmt_on_hunyuan_failure(self):
        result, mock_hunyuan, mock_tmt = self._call("apply", translation_ok=True)
        mock_tmt.assert_called_once()

    # ------------------------------------------------------------------
    # Word + translation unavailable (the new gate-removal behavior)
    # ------------------------------------------------------------------

    def test_word_without_translation_still_calls_hunyuan(self):
        """Hunyuan must be attempted even when translation failed."""
        result, mock_hunyuan, mock_tmt = self._call("apply", translation_ok=False)
        self.assertEqual(result["category"], "word")
        mock_hunyuan.assert_called_once_with("apply", None)

    def test_word_without_translation_skips_tmt(self):
        """TMT needs a translation to build template sentences — must be skipped."""
        result, mock_hunyuan, mock_tmt = self._call("apply", translation_ok=False)
        mock_tmt.assert_not_called()

    # ------------------------------------------------------------------
    # Phrase
    # ------------------------------------------------------------------

    def test_phrase_without_translation_calls_hunyuan(self):
        result, mock_hunyuan, mock_tmt = self._call("give up", translation_ok=False)
        self.assertEqual(result["category"], "phrase")
        mock_hunyuan.assert_called_once_with("give up", None)

    def test_phrase_without_translation_skips_tmt(self):
        result, mock_hunyuan, mock_tmt = self._call("give up", translation_ok=False)
        mock_tmt.assert_not_called()

    # ------------------------------------------------------------------
    # Sentence — must NOT generate examples (product semantic preserved)
    # ------------------------------------------------------------------

    def test_sentence_never_calls_hunyuan(self):
        result, mock_hunyuan, mock_tmt = self._call(
            "I love English.", translation_ok=True
        )
        self.assertEqual(result["category"], "sentence")
        mock_hunyuan.assert_not_called()
        mock_tmt.assert_not_called()

    def test_sentence_without_translation_never_calls_hunyuan(self):
        result, mock_hunyuan, mock_tmt = self._call(
            "I love English.", translation_ok=False
        )
        self.assertEqual(result["category"], "sentence")
        mock_hunyuan.assert_not_called()

    # ------------------------------------------------------------------
    # Hunyuan success propagates to response
    # ------------------------------------------------------------------

    def test_hunyuan_result_returned_in_response(self):
        from app.services.analyzer import analyze_text
        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": False, "translation": None, "provider": None}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_example_with_hunyuan",
                  return_value=("She craves success.", "她渴望成功。")),
            patch("app.services.analyzer._generate_example_with_tmt",
                  return_value=(None, None)),
        ):
            result = analyze_text("crave")
        self.assertEqual(result["exampleSentence"], "She craves success.")
        self.assertEqual(result["exampleTranslation"], "她渴望成功。")


class ValidatorClassificationTest(unittest.TestCase):
    """
    Phase 8H: verify that validate_english classifies inputs correctly,
    including the Rule-3 fix for hyphenated compound words.
    """

    def _cat(self, text: str) -> str:
        from app.services.validator import validate_english
        return validate_english(text)["category"]

    # ── Common words ──────────────────────────────────────────────
    def test_clutch_is_word(self):
        self.assertEqual(self._cat("clutch"), "word")

    def test_crave_is_word(self):
        self.assertEqual(self._cat("crave"), "word")

    # ── Phrases ───────────────────────────────────────────────────
    def test_break_a_leg_is_phrase(self):
        self.assertEqual(self._cat("break a leg"), "phrase")

    def test_pick_up_is_phrase(self):
        self.assertEqual(self._cat("pick up"), "phrase")

    def test_commit_guilty_is_phrase(self):
        self.assertEqual(self._cat("commit guilty"), "phrase")

    # ── Hyphenated compound words (Rule-3 fix) ────────────────────
    def test_well_known_is_word(self):
        self.assertEqual(self._cat("well-known"), "word")

    def test_full_time_is_word(self):
        self.assertEqual(self._cat("full-time"), "word")

    def test_part_time_is_word(self):
        self.assertEqual(self._cat("part-time"), "word")

    def test_up_to_date_is_word(self):
        self.assertEqual(self._cat("up-to-date"), "word")

    def test_state_of_the_art_is_word(self):
        self.assertEqual(self._cat("state-of-the-art"), "word")

    def test_long_term_is_word(self):
        self.assertEqual(self._cat("long-term"), "word")

    def test_self_control_is_word(self):
        self.assertEqual(self._cat("self-control"), "word")

    def test_e_mail_is_word(self):
        self.assertEqual(self._cat("e-mail"), "word")

    def test_co_worker_is_word(self):
        self.assertEqual(self._cat("co-worker"), "word")

    def test_follow_up_is_word(self):
        self.assertEqual(self._cat("follow-up"), "word")

    def test_check_in_is_word(self):
        self.assertEqual(self._cat("check-in"), "word")

    def test_make_up_is_word(self):
        self.assertEqual(self._cat("make-up"), "word")

    # ── Numbers / symbols — must stay unknown or error ────────────
    def test_2024_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("2024")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    def test_numeric_range_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("100-200")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    def test_negative_number_not_example_eligible(self):
        result = __import__("app.services.validator", fromlist=["validate_english"]).validate_english("-50")
        self.assertIn(result["category"], ("unknown",))
        self.assertEqual(result["level"], "error")

    # ── COVID-19 stays unknown (has digit) ────────────────────────
    def test_covid19_is_unknown(self):
        self.assertEqual(self._cat("COVID-19"), "unknown")


class HyphenatedWordExampleChainTest(unittest.TestCase):
    """
    Phase 8H: verify that hyphenated words (after Rule-3 fix) enter
    example generation, and that the gate logic is correct.
    """

    def _call_with_mocks(self, text):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "测试", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_example_with_hunyuan") as mock_hunyuan,
            patch("app.services.analyzer._generate_example_with_tmt") as mock_tmt,
        ):
            mock_hunyuan.return_value = (None, None)
            mock_tmt.return_value = (None, None)
            result = analyze_text(text)
            return result, mock_hunyuan, mock_tmt

    def test_well_known_calls_hunyuan(self):
        result, mock_hunyuan, _ = self._call_with_mocks("well-known")
        self.assertEqual(result["category"], "word")
        mock_hunyuan.assert_called_once_with("well-known", "测试")

    def test_full_time_calls_hunyuan(self):
        result, mock_hunyuan, _ = self._call_with_mocks("full-time")
        self.assertEqual(result["category"], "word")
        mock_hunyuan.assert_called_once()

    def test_follow_up_calls_hunyuan(self):
        result, mock_hunyuan, _ = self._call_with_mocks("follow-up")
        self.assertEqual(result["category"], "word")
        mock_hunyuan.assert_called_once()

    def test_unknown_category_skips_hunyuan(self):
        result, mock_hunyuan, mock_tmt = self._call_with_mocks("COVID-19")
        self.assertEqual(result["category"], "unknown")
        mock_hunyuan.assert_not_called()
        mock_tmt.assert_not_called()

    def test_hyphenated_word_hunyuan_result_propagated(self):
        from app.services.analyzer import analyze_text

        with (
            patch("app.services.analyzer.get_cache", return_value=None),
            patch("app.services.analyzer.set_cache"),
            patch("app.services.analyzer.translate_to_zh",
                  return_value={"ok": True, "translation": "全职", "provider": "tencent"}),
            patch("app.services.analyzer.generate_understanding", return_value="u"),
            patch("app.services.analyzer.generate_example_with_hunyuan",
                  return_value=("She works full-time.", "她全职工作。")),
            patch("app.services.analyzer._generate_example_with_tmt",
                  return_value=(None, None)),
        ):
            result = analyze_text("full-time")

        self.assertEqual(result["category"], "word")
        self.assertEqual(result["exampleSentence"], "She works full-time.")
        self.assertEqual(result["exampleTranslation"], "她全职工作。")


if __name__ == "__main__":
    unittest.main()
