"""CostTracker — the session usage/cost aggregator.

Synthesizes the reference accounting models:

- **Per-model accumulator** — keyed by model name, plus rolled-up session totals
  and a running USD cost. This is the primary shape, because it answers "how much
  did each model cost me this session".
- **Codex** — a ``last_usage`` snapshot + context-window-remaining estimate, so
  callers can show "% of window left" without re-counting the whole history.
- **Budget API** — the ``Costs`` namedtuple, ``max_budget``, and the
  ``update_cost`` / ``get_costs`` / ``get_total_*`` methods that ``base_llm``
  calls.

A single :class:`CostTracker` instance is owned by ``Context`` and shared by the
LLM clients it builds (one per pricing mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, NamedTuple, Optional

from mote.contracts.conversation.constants import MODEL_CONTEXT_WINDOW_DEFAULT
from mote.runtime.models.cost.pricing import PricingMode, cost_of
from mote.runtime.models.cost.usage import TokenUsage
from mote.runtime.telemetry.logging import logger


class Costs(NamedTuple):
    """Aggregate token/cost 4-tuple returned by ``base_llm.get_costs``."""

    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost: float
    total_budget: float

    @classmethod
    def zero(cls) -> "Costs":
        """An all-zero ``Costs`` for the no-cost-manager / empty case."""
        return cls(
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cost=0.0,
            total_budget=0.0,
        )


# Codex reserves a baseline (prompts + tools + room to compact) so the window
# reads ~100% right after the first prompt rather than already-partially-full.
BASELINE_TOKENS = 12_000


@dataclass
class ModelUsage:
    """Per-model accumulator."""

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

    def attributed_cost_usd(self) -> float:
        return self.total_cost

    def attributed_total_tokens(self) -> int:
        return self.total_token_usage().total_tokens

    def attributed_cost_is_estimated(self) -> bool:
        return self.has_unknown_model_cost

    # ------------------------------------------------------------------ record
    def add(self, usage: TokenUsage, model: Optional[str], *, context_window: int = 0) -> float:
        """Record one call's *usage* under *model*; returns its USD cost.

        No-ops on an all-zero usage so synthetic/placeholder calls don't pollute
        the per-model breakdown.
        """
        if usage is None or usage.is_zero():
            return 0.0
        model = model or "unknown"
        cost, known = cost_of(usage, model, self.mode)
        self._record(usage, model, cost, known=known, context_window=context_window)
        return cost

    def record_settled(
        self,
        usage: TokenUsage,
        model: Optional[str],
        cost_usd: float,
        *,
        context_window: int = 0,
    ) -> None:
        """Record usage whose authoritative cost was settled by ModelGateway."""

        if usage is None or usage.is_zero():
            return
        self._record(
            usage,
            model or "unknown",
            cost_usd,
            known=True,
            context_window=context_window,
        )

    def _record(
        self,
        usage: TokenUsage,
        model: str,
        cost: float,
        *,
        known: bool,
        context_window: int = 0,
    ) -> None:
        if not known:
            self.has_unknown_model_cost = True
            logger.warning(f"Model {model!r} not in pricing table; cost is estimated.")

        bucket = self.model_usage.get(model)
        if bucket is None:
            bucket = ModelUsage(context_window=context_window or context_window_for(model))
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

    # alias used by the richer call sites
    record = add

    # ----------------------------------------------- prompt/completion entry
    def update_cost(self, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        """Build a TokenUsage from raw prompt/completion counts and record it.

        The convenience entry for any caller that only has prompt/completion
        counts rather than a full ``TokenUsage``.
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

    # Stable property form used by provider adapters and status projections.
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
        usage_bucket = self.model_usage.get(model or "")
        window = (
            usage_bucket.context_window
            if usage_bucket is not None and usage_bucket.context_window > 0
            else context_window_for(model)
        )
        used = self.last_usage.total_tokens
        if window <= BASELINE_TOKENS:
            return {"window": window, "used": used, "remaining": 0, "percent_left": 0}
        effective = window - BASELINE_TOKENS
        used_over = max(0, used - BASELINE_TOKENS)
        remaining = max(0, effective - used_over)
        percent = round(min(100.0, max(0.0, remaining / effective * 100.0)))
        return {
            "window": window,
            "used": used,
            "remaining": remaining,
            "percent_left": percent,
        }


def context_window_for(model: Optional[str]) -> int:
    """Conservative fallback for calls lacking canonical endpoint metadata."""
    return MODEL_CONTEXT_WINDOW_DEFAULT
