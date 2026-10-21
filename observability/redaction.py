"""Scrub secrets from everything the team writes or sends (FR-219, research R12).

A secret is (a) the value, 8 or more characters long, of any environment variable whose
name contains ``KEY``, ``TOKEN``, ``SECRET``, ``PASSWORD`` or ``CREDENTIAL`` (any case),
and (b) any text shaped like an Anthropic API key, whether or not it is in the
environment. Events, logs, error messages, run records, checkpoints, QA reports, cost
reports and LangSmith traces all pass through one ``Redactor``.
"""

import os
import re
from collections.abc import Mapping

SECRET_NAME = re.compile("KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")
MARKER = "[REDACTED]"
MIN_SECRET_LENGTH = 8
"""Shorter values are ordinary words far more often than secrets, so they are kept."""


class Redactor:
    """Replaces known secret values and Anthropic-key-shaped text with ``MARKER``."""

    def __init__(self, secrets: list[str]) -> None:
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(set(secrets), key=len, reverse=True)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Redactor":
        """Collect the secret values from ``env`` (default: the process environment)."""
        env = os.environ if env is None else env
        return cls([
            value for name, value in env.items()
            if SECRET_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH
        ])

    def redact(self, value):
        """Return a copy of ``value`` with every secret replaced.

        Strings are scrubbed; dicts, lists and tuples are walked; other values (numbers,
        booleans, ``None``) are returned unchanged. The input is never modified.
        """
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value

    def _redact_text(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, MARKER)
        return ANTHROPIC_KEY_PATTERN.sub(MARKER, text)
