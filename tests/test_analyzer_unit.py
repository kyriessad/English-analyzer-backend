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


if __name__ == "__main__":
    unittest.main()
