"""CostTracker — the session usage/cost aggregator.

Synthesizes the three reference accounting models:

- **Claude Code** — a per-model accumulator (``ModelUsage`` keyed by model name)
  plus rolled-up session totals and a running USD cost. This is the primary
  shape, because it answers "how much did each model cost me this session".
- **Codex** — a ``last_usage`` snapshot + context-window-remaining estimate, so
  callers can show "% of window left" without re-counting the whole history.
- **Mote (legacy ``CostManager``)** — the ``Costs`` namedtuple, ``max_budget``,
  and the ``update_cost`` / ``get_costs`` / ``get_total_*`` API, kept as a
  drop-in shim so existing call sites (``base_llm``) need no behavioral change.

A single :class:`CostTracker` instance is owned by ``Context`` and shared by the
LLM clients it builds (one per pricing mode), exactly as the old ``CostManager``
was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, NamedTuple, Optional

from mote.common.const.context import MODEL_CONTEXT_WINDOW_DEFAULT
from mote.common.logs import logger
from mote.common.utils.token_counter import TOKEN_MAX
from mote.router.cost.pricing import PricingMode, cost_of
from mote.router.cost.usage import TokenUsage


class Costs(NamedTuple):
    """Legacy 4-tuple kept for backward compatibility with ``base_llm.get_costs``."""

    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost: float
    total_budget: float

    @classmethod
    def zero(cls) -> "Costs":
        """An all-zero ``Costs`` for the no-cost-manager / empty case."""
        return cls(total_prompt_tokens=0, total_completion_tokens=0, total_cost=0.0, total_budget=0.0)


# Codex reserves a baseline (prompts + tools + room to compact) so the window
# reads ~100% right after the first prompt rather than already-partially-full.
BASELINE_TOKENS = 12_000


@dataclass
class ModelUsage:
    """Per-model accumulator (Claude Code ``ModelUsage``)."""

    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    context_window: int = 0
    requests: int = 0


@dataclass
class CostTracker:
    """Accumulates token usage and USD cost across a session.

    Args:
        mode: Which pricing strategy to apply (standard / fireworks / free).
        max_budget: Soft budget ceiling in USD (informational; surfaced in logs).
        on_record: Optional sink invoked as ``(usage, model, cost)`` after each
            recorded call — used to fan out to telemetry without coupling.
    """

    mode: PricingMode = PricingMode.STANDARD
    max_budget: float = 10.0
    total_budget: float = 0.0
    on_record: Optional[Callable[[TokenUsage, str, float], None]] = None

    model_usage: dict[str, ModelUsage] = field(default_factory=dict)
    total_cost: float = 0.0
    last_usage: TokenUsage = field(default_factory=TokenUsage)
    last_model: Optional[str] = None
    last_cost: float = 0.0  # USD cost of the most recent recorded call
    has_unknown_model_cost: bool = False

    # ------------------------------------------------------------------ record
    def add(self, usage: TokenUsage, model: Optional[str]) -> float:
        """Record one call's *usage* under *model*; returns its USD cost.

        No-ops on an all-zero usage so synthetic/placeholder calls don't pollute
        the per-model breakdown.
        """
        if usage is None or usage.is_zero():
            return 0.0
        model = model or "unknown"
        cost, known = cost_of(usage, model, self.mode)
        if not known:
            self.has_unknown_model_cost = True
            logger.warning(f"Model {model!r} not in pricing table; cost is estimated.")

        bucket = self.model_usage.get(model)
        if bucket is None:
            bucket = ModelUsage(context_window=context_window_for(model))
            self.model_usage[model] = bucket
        bucket.usage.add(usage)
        bucket.cost_usd += cost
        bucket.requests += 1

        self.total_cost += cost
        self.last_usage = usage
        self.last_model = model
        self.last_cost = cost

        logger.debug(
            f"Total running cost: ${self.total_cost:.4f} | Max budget: ${self.max_budget:.3f} | "
            f"Current cost: ${cost:.4f}, input={usage.input_tokens} "
            f"(cached {usage.cached_input_tokens}), output={usage.output_tokens}"
        )
        if self.on_record is not None:
            try:
                self.on_record(usage, model, cost)
            except Exception as e:  # sink must never break the request path
                logger.warning(f"cost on_record sink failed: {e}")
        return cost

    # legacy alias used by the new richer call sites
    record = add

    # ------------------------------------------------- legacy CostManager shim
    def update_cost(self, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        """Backward-compatible entry: build a TokenUsage and record it.

        Mirrors the old ``CostManager.update_cost`` signature so any caller that
        only has prompt/completion counts keeps working.
        """
        self.add(
            TokenUsage(
                input_tokens=int(prompt_tokens or 0),
                output_tokens=int(completion_tokens or 0),
                total_tokens=int((prompt_tokens or 0) + (completion_tokens or 0)),
            ),
            model,
        )

    # ---------------------------------------------------------------- rollups
    def total_token_usage(self) -> TokenUsage:
        """Element-wise sum of usage across every model this session."""
        total = TokenUsage()
        for bucket in self.model_usage.values():
            total.add(bucket.usage)
        return total

    def get_total_cost(self) -> float:
        return self.total_cost

    def get_total_prompt_tokens(self) -> int:
        return self.total_token_usage().input_tokens

    def get_total_completion_tokens(self) -> int:
        return self.total_token_usage().output_tokens

    def get_costs(self) -> Costs:
        total = self.total_token_usage()
        return Costs(total.input_tokens, total.output_tokens, self.total_cost, self.total_budget)

    # legacy attribute access (old code read these off the pydantic model)
    @property
    def total_prompt_tokens(self) -> int:
        return self.get_total_prompt_tokens()

    @property
    def total_completion_tokens(self) -> int:
        return self.get_total_completion_tokens()

    # ----------------------------------------------- context-window (Codex)
    def context_remaining(self, model: Optional[str] = None) -> dict:
        """Codex-style remaining-window estimate from the last call's usage.

        Returns ``{window, used, remaining, percent_left}``. ``used`` is the last
        call's total tokens (its full context size); the baseline is subtracted
        from both sides so a fresh conversation reads ~100%.
        """
        model = model or self.last_model
        window = context_window_for(model)
        used = self.last_usage.total_tokens
        if window <= BASELINE_TOKENS:
            return {"window": window, "used": used, "remaining": 0, "percent_left": 0}
        effective = window - BASELINE_TOKENS
        used_over = max(0, used - BASELINE_TOKENS)
        remaining = max(0, effective - used_over)
        percent = round(min(100.0, max(0.0, remaining / effective * 100.0)))
        return {"window": window, "used": used, "remaining": remaining, "percent_left": percent}


def context_window_for(model: Optional[str]) -> int:
    """The model's context window (``TOKEN_MAX``), or the default if unknown."""
    if not model:
        return MODEL_CONTEXT_WINDOW_DEFAULT
    if model in TOKEN_MAX:
        return TOKEN_MAX[model]
    best = None
    for key in TOKEN_MAX:
        if key in model and (best is None or len(key) > len(best)):
            best = key
    return TOKEN_MAX[best] if best else MODEL_CONTEXT_WINDOW_DEFAULT
