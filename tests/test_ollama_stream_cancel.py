import json
import unittest
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = iter(lines)
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        return self._lines

    def close(self):
        self.closed = True


class OllamaStreamCancelTest(unittest.TestCase):
    def test_generator_close_closes_underlying_ollama_stream(self):
        """Aborting the stream (GeneratorExit) must run response.close().

        This is the backend half of the cancel feature: when the client aborts
        the NDJSON response, the generator chain is closed, which must release
        the underlying Ollama HTTP stream rather than leaking the connection.
        """
        from app.services.ollama_example import generate_analysis_with_ollama_stream

        line = json.dumps({
            "response": json.dumps({"meaning": "渴望"}, ensure_ascii=False),
            "done": False,
        }).encode("utf-8")

        fake = _FakeResponse([line])

        with patch("app.services.ollama_example._post_generate_stream", return_value=fake):
            gen = generate_analysis_with_ollama_stream("crave", "word")
            first = next(gen)
            self.assertEqual("field", first[0])
            self.assertFalse(fake.closed)
            gen.close()

        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
