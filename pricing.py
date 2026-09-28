"""Anthropic API model pricing.

Rates are USD per million tokens. Source: Anthropic public pricing page,
captured 2025-04-30. Update the comment date alongside any rate change.

NOTE: this file is a read-only consumer of pricing data. It MUST NOT
reference the framework's HR6 "extra_usage" field — quota / cost
enforcement uses plan-window / token_log data only.
"""

# extra_usage prohibition: see module docstring. The bare string below is
# the sole permitted reference.
# (search guard: extra_usage)

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


class UnknownModel(KeyError):
    """Raised by cost_for() when the model id is not in MODEL_PRICING."""


@dataclass(frozen=True)
class ModelRates:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# Rates as of 2026-09-23. USD per million tokens.
# Sources:
#   - claude-opus-5-5      :  4.00 / 20.00 / 0.20 /  5.00  (platform.claude.com models
#     overview, 2026-09-23: cache read is 5% of input on Opus 5.5; cache write
#     assumed at the standard 1.25x input)
#   - claude-opus-4-7      : 15.00 / 75.00 / 1.50 / 18.75
#   - claude-sonnet-4-6    :  3.00 / 15.00 / 0.30 /  3.75
#   - claude-haiku-4-5-... :  0.80 /  4.00 / 0.08 /  1.00
# When updating, also bump the date stamp above.
MODEL_PRICING: Dict[str, ModelRates] = {
    "claude-opus-5-5": ModelRates(
        input_per_mtok=4.00,
        output_per_mtok=20.00,
        cache_read_per_mtok=0.20,
        cache_write_per_mtok=5.00,
    ),
    "claude-opus-4-8": ModelRates(
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        cache_read_per_mtok=0.50,
        cache_write_per_mtok=6.25,
    ),
    "claude-opus-4-7": ModelRates(
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_read_per_mtok=1.50,
        cache_write_per_mtok=18.75,
    ),
    "claude-sonnet-4-6": ModelRates(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-haiku-4-5-20251001": ModelRates(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_read_per_mtok=0.08,
        cache_write_per_mtok=1.00,
    ),
}


_MTOK = 1_000_000.0


def cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Return USD spend for one usage record.

    Raises UnknownModel if `model` is not in MODEL_PRICING.
    """
    rates = MODEL_PRICING.get(model)
    if rates is None:
        raise UnknownModel(model)
    return (
        input_tokens * rates.input_per_mtok / _MTOK
        + output_tokens * rates.output_per_mtok / _MTOK
        + cache_read_tokens * rates.cache_read_per_mtok / _MTOK
        + cache_write_tokens * rates.cache_write_per_mtok / _MTOK
    )
