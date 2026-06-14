"""Human/machine-readable rendering of a :class:`CostTracker`.

Two reference styles are reproduced:

- **Claude Code** — the ``/cost`` block (total USD + per-model breakdown) and the
  status-line JSON (``cost`` + ``context_window`` objects piped to a user's
  status command).
- **Codex** — the terse end-of-run ``FinalOutput`` line
  (``Token usage: total=… input=… (+ N cached) output=… (reasoning N)``).
"""

from __future__ import annotations

from typing import Optional

from metagpt.router.cost.tracker import CostTracker


def format_cost(cost: float) -> str:
    """Claude Code's adaptive precision: 2 dp above $0.50, else 4 dp."""
    return f"${cost:.2f}" if cost >= 0.5 else f"${cost:.4f}"


def format_model_usage(tracker: CostTracker) -> str:
    """Per-model token + cost breakdown (one line per model)."""
    if not tracker.model_usage:
        return "  (no usage recorded)"
    lines = []
    for model in sorted(tracker.model_usage, key=lambda m: -tracker.model_usage[m].usage.total_tokens):
        b = tracker.model_usage[model]
        u = b.usage
        lines.append(
            f"  {model}: {u.input_tokens} input "
            f"({u.cached_input_tokens} cache read, {u.cache_creation_tokens} cache write), "
            f"{u.output_tokens} output → {format_cost(b.cost_usd)}"
        )
    return "\n".join(lines)


def format_total_cost(tracker: CostTracker) -> str:
    """The Claude Code ``/cost`` summary block."""
    total = tracker.total_token_usage()
    estimated = " (includes estimated cost for unknown models)" if tracker.has_unknown_model_cost else ""
    return (
        f"Total cost: {format_cost(tracker.total_cost)}{estimated}\n"
        f"Total tokens: {total.total_tokens} "
        f"({total.input_tokens} input, {total.output_tokens} output)\n"
        f"Usage by model:\n{format_model_usage(tracker)}"
    )


def final_output(tracker: CostTracker) -> str:
    """Codex-style one-line token summary over the whole session."""
    u = tracker.total_token_usage()
    return (
        f"Token usage: total={u.total_tokens} "
        f"input={u.input_tokens} (+ {u.cached_input_tokens} cached) "
        f"output={u.output_tokens} (reasoning {u.reasoning_tokens})"
    )


def status_line_dict(tracker: CostTracker, model: Optional[str] = None) -> dict:
    """Claude Code status-line JSON: ``cost`` + ``context_window`` objects."""
    total = tracker.total_token_usage()
    ctx = tracker.context_remaining(model)
    return {
        "cost": {
            "total_cost_usd": tracker.total_cost,
            "total_input_tokens": total.input_tokens,
            "total_output_tokens": total.output_tokens,
            "total_cache_read_tokens": total.cached_input_tokens,
            "total_cache_creation_tokens": total.cache_creation_tokens,
        },
        "context_window": {
            "context_window_size": ctx["window"],
            "current_usage": ctx["used"],
            "remaining": ctx["remaining"],
            "remaining_percentage": ctx["percent_left"],
        },
    }
