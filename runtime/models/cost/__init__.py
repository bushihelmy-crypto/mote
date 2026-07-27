"""Cost / token / usage accounting for the router LLM layer.

Per-model USD cost tracking plus token-only context-window accounting for the
router LLM layer.

Public surface:
    - :class:`TokenUsage` / ``EMPTY_USAGE`` — normalized usage record + adapters.
    - :class:`ModelPricing`, :class:`PricingMode`, :func:`cost_of`,
      :func:`lookup_pricing` — cache-aware USD pricing.
    - :class:`CostTracker`, :class:`ModelUsage`, :class:`Costs` — session
      aggregation.
    - report helpers — :func:`format_total_cost`, :func:`format_model_usage`,
      :func:`final_output`, :func:`status_line_dict`, :func:`format_cost`.
"""

from mote.runtime.models.cost.node import CostNode
from mote.runtime.models.cost.pricing import (
    DEFAULT_UNKNOWN_PRICING,
    PRICING,
    ModelPricing,
    PricingMode,
    cost_of,
    lookup_pricing,
    resolve_pricing,
)
from mote.runtime.models.cost.report import (
    final_output,
    format_cost,
    format_cost_tree,
    format_model_usage,
    format_total_cost,
    status_line_dict,
)
from mote.runtime.models.cost.tracker import BASELINE_TOKENS, Costs, CostTracker, ModelUsage, context_window_for
from mote.runtime.models.cost.usage import EMPTY_USAGE, TokenUsage

__all__ = [
    # usage
    "TokenUsage",
    "EMPTY_USAGE",
    # pricing
    "ModelPricing",
    "PricingMode",
    "PRICING",
    "DEFAULT_UNKNOWN_PRICING",
    "cost_of",
    "lookup_pricing",
    "resolve_pricing",
    # tracker
    "CostTracker",
    "ModelUsage",
    "Costs",
    "BASELINE_TOKENS",
    "context_window_for",
    # node (fleet cost mirror tree)
    "CostNode",
    # report
    "format_cost",
    "format_cost_tree",
    "format_model_usage",
    "format_total_cost",
    "final_output",
    "status_line_dict",
]
