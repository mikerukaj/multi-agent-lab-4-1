"""Token prices and the cost of one model call (FR-222, research R9).

Prices come from a dated table (``cost/prices.json``, or ``PRICE_TABLE_PATH``), per
million tokens. A call is priced by the model that actually served it
(``response.model``), so a refusal fallback is billed at its own rates. A model missing
from the table has no price: its cost is ``None``, never a guess.
"""

import json
from collections.abc import Mapping
from pathlib import Path

DEFAULT_PRICE_TABLE = Path(__file__).resolve().parent / "prices.json"
PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
MILLION = 1_000_000


def price_table_path(env: Mapping[str, str]) -> Path:
    """``PRICE_TABLE_PATH``, or the table shipped with the project."""
    return Path(env.get("PRICE_TABLE_PATH") or DEFAULT_PRICE_TABLE)


def load_price_table(path: Path) -> dict:
    """Read and check a price table.

    Raises:
        ValueError: if the table has no ``version``, or a price is missing or negative.
        OSError: if the file can't be read.
    """
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    if not table.get("version"):
        raise ValueError(f"the price table {path} has no version")
    for model, prices in table.get("models", {}).items():
        tiers = [prices] + ([prices["long_context"]] if prices.get("long_context") else [])
        for tier in tiers:
            for key in PRICE_KEYS:
                if not isinstance(tier.get(key), (int, float)) or tier[key] < 0:
                    raise ValueError(f"the price table gives {model} no valid {key} price")
    return table


def is_priced(model: str, table: dict) -> bool:
    """Whether ``model`` has prices in ``table``."""
    return model in table.get("models", {})


def cost_of(
    usage: Mapping[str, int],
    model: str,
    ttl: str,
    table: dict,
    cache_writes: Mapping[str, int] | None = None,
) -> float | None:
    """The USD cost of one call's ``usage`` on ``model``, or ``None`` if it is unpriced.

    Cache writes are split into 5-minute and 1-hour writes by ``cache_writes``
    (``ephemeral_5m_input_tokens``/``ephemeral_1h_input_tokens`` from the response) when
    given, otherwise all are priced at ``ttl``'s rate. A prompt over a model's
    long-context threshold (all input, cache reads and writes counted) uses that tier.
    """
    prices = table.get("models", {}).get(model)
    if prices is None:
        return None
    uncached = usage.get("input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    long_context = prices.get("long_context")
    if long_context and uncached + read + written > long_context["threshold_prompt_tokens"]:
        prices = long_context
    if cache_writes:
        five = cache_writes.get("ephemeral_5m_input_tokens", 0)
        hour = cache_writes.get("ephemeral_1h_input_tokens", 0)
    else:
        five, hour = (0, written) if ttl == "1h" else (written, 0)
    total = (uncached * prices["input"] + usage.get("output_tokens", 0) * prices["output"]
             + read * prices["cache_read"] + five * prices["cache_write_5m"]
             + hour * prices["cache_write_1h"])
    return round(total / MILLION, 8)
