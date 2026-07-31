"""Usage status-line formatting."""

from __future__ import annotations

from mote.product.presentation.events import UsageUpdated

USAGE_SEP = " \u2502 "


def format_usage_line(ev: UsageUpdated) -> str:
    """Compose a compact usage summary from a projected usage event."""
    parts: list[str] = []
    if ev.model:
        parts.append(str(ev.model))
    if ev.total_tokens:
        parts.append(f"{ev.total_tokens:,} tok")
    elif ev.input_tokens or ev.output_tokens:
        parts.append(f"{ev.input_tokens:,}→{ev.output_tokens:,} tok")
    if ev.cost_usd is not None:
        parts.append(f"${ev.cost_usd:.4f}")
    if ev.context_pct is not None:
        parts.append(f"ctx {ev.context_pct * 100:.0f}%")
    elif ev.context_used is not None and ev.context_window:
        parts.append(f"ctx {ev.context_used:,}/{ev.context_window:,}")
    return USAGE_SEP.join(parts)


__all__ = ["USAGE_SEP", "format_usage_line"]
