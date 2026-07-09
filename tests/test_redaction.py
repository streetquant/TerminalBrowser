import unittest

from termdeck.redaction import redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_common_secret_like_values(self):
        result = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz\napi_key=sk_test_1234567890abcdef")
        self.assertGreaterEqual(result.replacements, 2)
        labels = set(result.labels)
        self.assertTrue({"Authorization header", "Bearer token"} & labels)
        self.assertTrue({"API key assignment", "credential assignment"} & labels)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", result.text)

    def test_plain_text_is_unchanged(self):
        text = "pytest passed with 42 tests"
        result = redact_text(text)
        self.assertEqual(result.text, text)
        self.assertEqual(result.replacements, 0)
        self.assertEqual(result.labels, ())


if __name__ == "__main__":
    unittest.main()
