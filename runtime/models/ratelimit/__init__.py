"""Rate-limit tracking — provider quota visibility (the ``/usage`` limit side).

The counterpart to :mod:`router.cost`: where cost tracks accumulated per-agent
spend, this tracks the provider's *account-wide* rate-limit quota as reported in
each response's ``*-ratelimit-*`` headers. A single shared
:class:`RateLimitTracker` on the router :class:`~router.llm.context.Context`
records the latest snapshot per ``(provider, model)`` endpoint (last-write-wins);
the ``/usage`` command renders it via :func:`format_rate_limits`.

Capture is passive: an httpx response event-hook installed on each provider's
SDK client reads the headers off every response (success or error, streaming or
not) and feeds them to the tracker — no per-call-site plumbing, no extra request.
"""

from mote.runtime.models.ratelimit.report import format_rate_limits, format_snapshot
from mote.runtime.models.ratelimit.snapshot import RateLimitSnapshot
from mote.runtime.models.ratelimit.tracker import RateLimitTracker

__all__ = [
    "RateLimitSnapshot",
    "RateLimitTracker",
    "format_rate_limits",
    "format_snapshot",
]
