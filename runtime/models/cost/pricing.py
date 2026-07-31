"""Runtime cache-aware USD pricing for explicitly supported billing classes.

Prices every token bucket independently (input / output / cache
*write* / cache *read*) at a per-million-token rate, because cache reads are an
order of magnitude cheaper than fresh input and cache writes are ~25% dearer.
Codex, by contrast, computes no dollars at all. We adopt this dollar model and
Unknown provider deployments use a conservative estimated tier; canonical
gateway settlements remain authoritative.

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


@dataclass(frozen=True)
class ModelPricing:
    """USD per **one million** tokens, per bucket."""

    input: float
    output: float
    cache_write: float = 0.0
    cache_read: float = 0.0


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------
# Explicit cache-aware rates for supported billing classes (per Mtok).
_EXPLICIT_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(5.0, 15.0, 6.25, 0.5),
    "gpt-4o-mini": ModelPricing(0.15, 0.6, 0.1875, 0.015),
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
    return dict(_EXPLICIT_PRICING)


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
        row = {"prompt": 0.4, "completion": 1.6}
    else:
        size = _model_size(model)
        if 0 < size <= 16:
            row = {"prompt": 0.2, "completion": 0.8}
        elif 16 < size <= 80:
            row = {"prompt": 0.7, "completion": 2.8}
        else:
            row = {"prompt": 0.0, "completion": 0.0}
    # Fireworks has no cache tiers; reads/writes priced as fresh input.
    return ModelPricing(
        input=row["prompt"],
        output=row["completion"],
        cache_write=row["prompt"],
        cache_read=row["prompt"],
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
