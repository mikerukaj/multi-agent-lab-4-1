"""How many times to retry a temporary failure, and how long to wait between tries.

Directive 2 and FR-204: at most 3 retries by default, exponential backoff with jitter,
each wait capped (60 s by default), and a wait the service asks for (``retry-after``)
honoured when it fits under the cap (contracts/resilience.md, "Backoff").
"""

import random
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits shared by every guarded call in a run."""

    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RetryPolicy":
        """Read ``RETRY_MAX_RETRIES``, ``RETRY_BASE_DELAY_SECONDS``, ``RETRY_MAX_DELAY_SECONDS``.

        Raises:
            ValueError: naming the variable, if a value is not a number in its range.
        """
        defaults = cls()
        return cls(
            max_retries=int(
                number_setting(env, "RETRY_MAX_RETRIES", defaults.max_retries, 0, 5, int)
            ),
            base_delay=number_setting(
                env, "RETRY_BASE_DELAY_SECONDS", defaults.base_delay, 0.1, 10
            ),
            max_delay=number_setting(
                env, "RETRY_MAX_DELAY_SECONDS", defaults.max_delay, 1, 300
            ),
        )


def backoff_delay(
    policy: RetryPolicy, attempt: int, retry_after: float | None, rng: random.Random
) -> float | None:
    """Return the wait before retry number ``attempt + 1``, or ``None`` to stop retrying.

    Equal jitter: ``ceiling = min(max_delay, base_delay * 2**attempt)`` and the wait is
    drawn from ``[ceiling / 2, ceiling]``. A ``retry_after`` within ``max_delay`` raises
    the wait to at least that value; one above it means the run can't afford to wait,
    so there is no retry.
    """
    if retry_after is not None and retry_after > policy.max_delay:
        return None
    ceiling = min(policy.max_delay, policy.base_delay * 2**attempt)
    delay = ceiling / 2 + rng.uniform(0, ceiling / 2)
    return max(retry_after, delay) if retry_after is not None else delay


def number_setting(
    env: Mapping[str, str], name: str, default: float, low: float, high: float, kind=float
) -> float:
    """Read one numeric setting from ``env`` and check it is within ``[low, high]``.

    Shared by every resilience setting (retries, circuit breakers, response cache).

    Raises:
        ValueError: naming ``name``, if the value is malformed or out of range.
    """
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = kind(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number from {low} to {high}, not {raw!r}") from error
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low} to {high}, not {raw!r}")
    return value
