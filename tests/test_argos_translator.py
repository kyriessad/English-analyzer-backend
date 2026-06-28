import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.providers.argos_translator import ArgosTranslator


class FakeLanguage:
    def __init__(self, code, translation=None):
        self.code = code
        self._translation = translation

    def get_translation(self, target_language):
        return self._translation


class ArgosTranslatorTest(unittest.TestCase):
    def setUp(self):
        ArgosTranslator.reset_cache_for_tests()

    def tearDown(self):
        ArgosTranslator.reset_cache_for_tests()

    def _patch_languages(self, translation):
        fake_argos = SimpleNamespace(
            get_installed_languages=Mock(
                return_value=[
                    FakeLanguage("en", translation),
                    FakeLanguage("zh"),
                ]
            )
        )
        return patch("app.providers.argos_translator.argos_translate", fake_argos)

    def test_empty_string_returns_none(self):
        self.assertIsNone(ArgosTranslator().translate_to_zh("   "))

    def test_normal_english_text(self):
        translation = Mock()
        translation.translate.return_value = "我每天学习英语。"
        with self._patch_languages(translation):
            self.assertEqual(
                ArgosTranslator().translate_to_zh("I study English every day."),
                "我每天学习英语。",
            )

    def test_model_not_installed(self):
        fake_argos = SimpleNamespace(get_installed_languages=Mock(return_value=[]))
        with patch("app.providers.argos_translator.argos_translate", fake_argos):
            self.assertIsNone(ArgosTranslator().translate_to_zh("hello"))

    def test_translation_exception(self):
        translation = Mock()
        translation.translate.side_effect = RuntimeError("boom")
        with self._patch_languages(translation):
            self.assertIsNone(ArgosTranslator().translate_to_zh("hello"))

    def test_empty_translation_result(self):
        translation = Mock()
        translation.translate.return_value = "   "
        with self._patch_languages(translation):
            self.assertIsNone(ArgosTranslator().translate_to_zh("hello"))

    def test_init_does_not_lookup_or_download_model(self):
        fake_argos = SimpleNamespace(get_installed_languages=Mock(return_value=[]))
        with patch("app.providers.argos_translator.argos_translate", fake_argos):
            ArgosTranslator()
            fake_argos.get_installed_languages.assert_not_called()

    def test_translation_object_is_cached(self):
        translation = Mock()
        translation.translate.side_effect = ["你好", "世界"]
        with self._patch_languages(translation) as patched:
            translator = ArgosTranslator()
            self.assertEqual(translator.translate_to_zh("hello"), "你好")
            self.assertEqual(translator.translate_to_zh("world"), "世界")
            patched.get_installed_languages.assert_called_once()
            self.assertEqual(translation.translate.call_count, 2)


class ArgosRecoveryTest(unittest.TestCase):
    """Fix 3: Argos lookup failure must not be permanently cached."""

    def setUp(self):
        ArgosTranslator.reset_cache_for_tests()

    def tearDown(self):
        ArgosTranslator.reset_cache_for_tests()

    def test_first_call_model_missing_second_call_succeeds(self):
        """First call model not installed → None; second call model installed → success. No reset_cache."""
        translation = Mock()
        translation.translate.return_value = "你好"

        # First call: no languages installed
        fake_empty = SimpleNamespace(get_installed_languages=Mock(return_value=[]))
        with patch("app.providers.argos_translator.argos_translate", fake_empty):
            result1 = ArgosTranslator().translate_to_zh("hello")
        self.assertIsNone(result1)

        # Second call: model now installed — must succeed WITHOUT reset_cache
        fake_with_model = SimpleNamespace(
            get_installed_languages=Mock(
                return_value=[
                    FakeLanguage("en", translation),
                    FakeLanguage("zh"),
                ]
            )
        )
        with patch("app.providers.argos_translator.argos_translate", fake_with_model):
            result2 = ArgosTranslator().translate_to_zh("hello")

        self.assertEqual(result2, "你好")

    def test_after_success_lookup_only_once(self):
        """After a successful lookup, subsequent calls use the cache."""
        translation = Mock()
        translation.translate.return_value = "我每天学习英语。"

        fake = SimpleNamespace(
            get_installed_languages=Mock(
                return_value=[
                    FakeLanguage("en", translation),
                    FakeLanguage("zh"),
                ]
            )
        )
        with patch("app.providers.argos_translator.argos_translate", fake):
            ArgosTranslator().translate_to_zh("I study English every day.")
            ArgosTranslator().translate_to_zh("Hello world")

        # get_installed_languages called only once despite two translate calls
        fake.get_installed_languages.assert_called_once()

    def test_first_lookup_throws_second_recovers(self):
        """Exception during first lookup → None; second call recovers without reset_cache."""
        translation = Mock()
        translation.translate.return_value = "你好"

        # First call: get_installed_languages throws
        fake_broken = SimpleNamespace(
            get_installed_languages=Mock(side_effect=RuntimeError("temporary error"))
        )
        with patch("app.providers.argos_translator.argos_translate", fake_broken):
            result1 = ArgosTranslator().translate_to_zh("hello")
        self.assertIsNone(result1)

        # Second call: normal — must succeed
        fake_ok = SimpleNamespace(
            get_installed_languages=Mock(
                return_value=[
                    FakeLanguage("en", translation),
                    FakeLanguage("zh"),
                ]
            )
        )
        with patch("app.providers.argos_translator.argos_translate", fake_ok):
            result2 = ArgosTranslator().translate_to_zh("hello")

        self.assertEqual(result2, "你好")

    def test_init_never_triggers_download_or_lookup(self):
        """init / import never triggers model download or language lookup."""
        fake = SimpleNamespace(get_installed_languages=Mock(return_value=[]))
        with patch("app.providers.argos_translator.argos_translate", fake):
            ArgosTranslator()
            fake.get_installed_languages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
