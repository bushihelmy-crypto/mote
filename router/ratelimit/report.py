"""Human-readable rendering of a :class:`RateLimitTracker` (the ``/usage`` block).

Mirrors :mod:`router.cost.report`: pure formatting over the tracker's current
snapshots, one line per ``(provider, model)`` endpoint showing the remaining /
limit for both the request and token quotas plus any reset hint. Absent fields
render as ``?`` so a partially-populated snapshot still reads cleanly.
"""

from __future__ import annotations

from typing import Optional

from mote.router.ratelimit.snapshot import RateLimitSnapshot
from mote.router.ratelimit.tracker import RateLimitTracker


def _quota(remaining: Optional[int], limit: Optional[int]) -> str:
    """Render a ``remaining/limit`` pair, using ``?`` for unknown sides."""
    r = str(remaining) if remaining is not None else "?"
    lim = str(limit) if limit is not None else "?"
    return f"{r}/{lim}"


def _reset(seconds: Optional[float], raw: Optional[str]) -> str:
    """Render the reset hint: OpenAI seconds preferred, else the raw Anthropic stamp."""
    if seconds is not None:
        return f"resets in {seconds:g}s"
    if raw:
        return f"resets at {raw}"
    return ""


def format_snapshot(snap: RateLimitSnapshot) -> str:
    """One endpoint's rate-limit line: requests + tokens quotas and reset hints."""
    parts = [
        f"  {snap.provider}/{snap.model}:",
        f"requests {_quota(snap.remaining_requests, snap.limit_requests)}",
    ]
    req_reset = _reset(snap.reset_requests_seconds, snap.reset_requests_raw)
    if req_reset:
        parts.append(f"({req_reset})")
    parts.append(f"| tokens {_quota(snap.remaining_tokens, snap.limit_tokens)}")
    tok_reset = _reset(snap.reset_tokens_seconds, snap.reset_tokens_raw)
    if tok_reset:
        parts.append(f"({tok_reset})")
    if snap.retry_after_seconds is not None:
        parts.append(f"| retry-after {snap.retry_after_seconds:g}s")
    return " ".join(parts)


def format_rate_limits(tracker: RateLimitTracker) -> str:
    """The ``/usage`` rate-limit block: a header + one line per observed endpoint."""
    if tracker.is_empty():
        return "Rate limits: (none reported yet)"
    lines = ["Rate limits (latest observed):"]
    lines.extend(format_snapshot(snap) for snap in tracker.snapshots())
    return "\n".join(lines)


__all__ = ["format_rate_limits", "format_snapshot"]
