"""Cache-aware USD pricing, bridged to Mote's tables.

Prices every token bucket independently (input / output / cache
*write* / cache *read*) at a per-million-token rate, because cache reads are an
order of magnitude cheaper than fresh input and cache writes are ~25% dearer.
Codex, by contrast, computes no dollars at all. We adopt this dollar model and
feed it from Mote's existing ``TOKEN_COSTS`` (per-1k) so no rate is lost.

Three pricing *modes*:

- ``STANDARD``  — table lookup with cache-aware math.
- ``FIREWORKS`` — model-size graded rates.
- ``FREE``      — self-hosted / open models cost nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from mote.common.utils.token_counter import FIREWORKS_GRADE_TOKEN_COSTS, TOKEN_COSTS

# Anthropic-style derivation factors for models whose table only lists
# input/output (no explicit cache rates): cache writes cost 1.25x fresh input,
# cache reads cost 0.1x. Mirrors Anthropic's tier ratios.
_CACHE_WRITE_FACTOR = 1.25
_CACHE_READ_FACTOR = 0.1


@dataclass(frozen=True)
class ModelPricing:
    """USD per **one million** tokens, per bucket."""

    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0

    @classmethod
    def from_per_1k(cls, prompt: float, completion: float) -> "ModelPricing":
        """Bridge a Mote ``TOKEN_COSTS`` row (per-1k) into per-Mtok rates."""
        input_m = prompt * 1000.0
        output_m = completion * 1000.0
        return cls(
            input=input_m,
            output=output_m,
            cache_write=round(input_m * _CACHE_WRITE_FACTOR, 6),
            cache_read=round(input_m * _CACHE_READ_FACTOR, 6),
        )


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------
# Explicit cache-aware rates for the headline models (per
# Mtok). These take precedence over the bridged ``TOKEN_COSTS`` entries.
_EXPLICIT_PRICING: dict[str, ModelPricing] = {
    # Claude 3.5/3.7/4.x Sonnet tier: 3 / 15 / 3.75 / 0.3
    "claude-3-5-sonnet": ModelPricing(3.0, 15.0, 3.75, 0.3),
    "claude-3.5-sonnet": ModelPricing(3.0, 15.0, 3.75, 0.3),
    "claude-3-7-sonnet": ModelPricing(3.0, 15.0, 3.75, 0.3),
    "claude-sonnet-4": ModelPricing(3.0, 15.0, 3.75, 0.3),
    "claude-4-sonnet": ModelPricing(3.0, 15.0, 3.75, 0.3),
    # Opus 4 / 4.1 tier: 15 / 75 / 18.75 / 1.5
    "claude-3-opus": ModelPricing(15.0, 75.0, 18.75, 1.5),
    "claude-opus-4": ModelPricing(15.0, 75.0, 18.75, 1.5),
    # Haiku tiers
    "claude-3-5-haiku": ModelPricing(0.8, 4.0, 1.0, 0.08),
    "claude-3.5-haiku": ModelPricing(0.8, 4.0, 1.0, 0.08),
    "claude-haiku-4": ModelPricing(1.0, 5.0, 1.25, 0.1),
}


def _build_table() -> dict[str, ModelPricing]:
    """Merge bridged ``TOKEN_COSTS`` rows with the explicit cache-aware rates."""
    table: dict[str, ModelPricing] = {}
    for model, row in TOKEN_COSTS.items():
        table[model] = ModelPricing.from_per_1k(row["prompt"], row["completion"])
    table.update(_EXPLICIT_PRICING)
    return table


PRICING: dict[str, ModelPricing] = _build_table()

# Unknown models fall back to a mid-tier Sonnet-like rate (a
# default tier rather than billing zero, so cost is never silently dropped).
DEFAULT_UNKNOWN_PRICING = ModelPricing(3.0, 15.0, 3.75, 0.3)


class PricingMode(str, Enum):
    STANDARD = "standard"
    FIREWORKS = "fireworks"
    FREE = "free"


def lookup_pricing(model: Optional[str]) -> tuple[ModelPricing, bool]:
    """Resolve a model name to ``(pricing, is_known)``.

    Tries an exact match, then a longest-prefix containment match (so
    ``claude-opus-4-20250101`` resolves to the ``claude-opus-4`` tier). Returns
    ``(DEFAULT_UNKNOWN_PRICING, False)`` when nothing matches.
    """
    if not model:
        return DEFAULT_UNKNOWN_PRICING, False
    if model in PRICING:
        return PRICING[model], True
    # Longest known key contained in the model name wins (handles dated suffixes
    # and provider prefixes like ``anthropic/claude-...``).
    best_key: Optional[str] = None
    for key in PRICING:
        if key in model and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is not None:
        return PRICING[best_key], True
    return DEFAULT_UNKNOWN_PRICING, False


def _fireworks_pricing(model: str) -> ModelPricing:
    """Model-size graded Fireworks rates.

    Fireworks bills by parameter-count band; the grade table is already per-Mtok.
    """

    def _model_size(name: str) -> float:
        m = re.findall(r".*-([0-9.]+)b", name)
        return float(m[0]) if m else -1.0

    if "mixtral-8x7b" in model:
        row = FIREWORKS_GRADE_TOKEN_COSTS["mixtral-8x7b"]
    else:
        size = _model_size(model)
        if 0 < size <= 16:
            row = FIREWORKS_GRADE_TOKEN_COSTS["16"]
        elif 16 < size <= 80:
            row = FIREWORKS_GRADE_TOKEN_COSTS["80"]
        else:
            row = FIREWORKS_GRADE_TOKEN_COSTS["-1"]
    # Fireworks has no cache tiers; reads/writes priced as fresh input.
    return ModelPricing(
        input=row["prompt"], output=row["completion"], cache_write=row["prompt"], cache_read=row["prompt"]
    )


def resolve_pricing(model: Optional[str], mode: PricingMode) -> tuple[ModelPricing, bool]:
    """Resolve pricing for a model under a given :class:`PricingMode`."""
    if mode == PricingMode.FREE:
        return ModelPricing(0.0, 0.0, 0.0, 0.0), True
    if mode == PricingMode.FIREWORKS:
        return _fireworks_pricing(model or ""), True
    return lookup_pricing(model)


def cost_of(usage, model: Optional[str], mode: PricingMode = PricingMode.STANDARD) -> tuple[float, bool]:
    """Compute the USD cost of a :class:`TokenUsage` under *model* / *mode*.

    Returns ``(cost_usd, is_known)`` where ``is_known`` is False when the model
    was not in the pricing table (so the caller can flag estimated costs). Cache
    reads/writes are billed at their own rates; the remaining (non-cached) input
    at the full input rate.
    """
    pricing, known = resolve_pricing(model, mode)
    cost = (
        usage.non_cached_input() / 1e6 * pricing.input
        + usage.cached_input_tokens / 1e6 * pricing.cache_read
        + usage.cache_creation_tokens / 1e6 * pricing.cache_write
        + usage.output_tokens / 1e6 * pricing.output
    )
    return cost, known
