"""Tests for ``observability.redaction`` (FR-219, research R12)."""

import unittest

from observability.redaction import MARKER, Redactor

ENV = {
    "MY_API_KEY": "key-value-123456",
    "GITHUB_TOKEN": "ghp_tokenvalue987",
    "DB_PASSWORD": "hunter2hunter2",
    "x_secret": "lowercase-secret-1",
    "AWS_CREDENTIAL": "aws-credential-xyz",
    "SHORT_KEY": "abcde",
    "EMPTY_TOKEN": "",
    "HOME": "/home/learner",
    "PATH": "/usr/bin:/bin",
}
FAKE_ANTHROPIC_KEY = "sk-ant-api03-abcdefghIJKLMNOP_qrstuv-0123"


class RedactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor.from_env(ENV)

    def test_every_secret_name_pattern_is_redacted_in_nested_values(self) -> None:
        value = {
            "message": f"key={ENV['MY_API_KEY']} token={ENV['GITHUB_TOKEN']}",
            "items": [ENV["DB_PASSWORD"], {"deep": f"x {ENV['x_secret']} y"}],
            "credential": ENV["AWS_CREDENTIAL"],
        }
        redacted = self.redactor.redact(value)
        self.assertEqual(
            redacted,
            {
                "message": f"key={MARKER} token={MARKER}",
                "items": [MARKER, {"deep": f"x {MARKER} y"}],
                "credential": MARKER,
            },
        )

    def test_short_and_empty_values_are_not_treated_as_secrets(self) -> None:
        self.assertEqual(self.redactor.redact("abcde and more"), "abcde and more")
        self.assertEqual(self.redactor.redact(""), "")

    def test_anthropic_shaped_keys_are_redacted_even_outside_the_environment(self) -> None:
        text = f"echoed {FAKE_ANTHROPIC_KEY} here"
        self.assertEqual(Redactor.from_env({}).redact(text), f"echoed {MARKER} here")

    def test_non_secret_variables_are_untouched(self) -> None:
        text = f"home {ENV['HOME']} path {ENV['PATH']}"
        self.assertEqual(self.redactor.redact(text), text)

    def test_redact_returns_a_new_object_and_leaves_the_input_unchanged(self) -> None:
        value = {"list": [ENV["MY_API_KEY"]], "n": 3, "none": None, "flag": True}
        before = {"list": [ENV["MY_API_KEY"]], "n": 3, "none": None, "flag": True}
        redacted = self.redactor.redact(value)
        self.assertEqual(value, before)
        self.assertIsNot(redacted, value)
        self.assertEqual(redacted, {"list": [MARKER], "n": 3, "none": None, "flag": True})

    def test_longer_secrets_are_replaced_before_their_substrings(self) -> None:
        redactor = Redactor.from_env({"A_KEY": "abcdefgh", "B_KEY": "abcdefgh-longer"})
        self.assertEqual(redactor.redact("abcdefgh-longer"), MARKER)

    def test_tuples_are_redacted_as_lists_of_values(self) -> None:
        self.assertEqual(self.redactor.redact((ENV["MY_API_KEY"], 1)), (MARKER, 1))


if __name__ == "__main__":
    unittest.main()
