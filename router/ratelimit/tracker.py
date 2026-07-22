"""RateLimitTracker — the fleet's rolling rate-limit state, keyed by endpoint.

A single shared instance lives on the router :class:`~router.llm.context.Context`
(account-wide state, unlike the per-agent :class:`~router.cost.CostTracker`), so
every provider client observing a response updates the same map. Each response's
headers overwrite the prior snapshot for their ``(provider, model)`` key —
last-write-wins, because the newest response carries the freshest quota.

The tracker is intentionally tiny: observe a snapshot, read the current map. No
lineage, no summing — rate limits are provider account state, not per-agent spend.
``observe_headers`` is the one write seam the response hook calls; it is fully
best-effort (a parse failure or missing headers is a silent no-op) so it can sit
on the hot response path without ever risking a live call.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from mote.router.ratelimit.snapshot import RateLimitSnapshot


class RateLimitTracker:
    """Latest rate-limit snapshot per ``(provider, model)`` endpoint."""

    def __init__(self) -> None:
        self._snapshots: Dict[Tuple[str, str], RateLimitSnapshot] = {}

    def observe_headers(self, provider: str, model: str, headers: Mapping[str, str]) -> None:
        """Record the rate-limit headers from one response (best-effort, no-op on miss).

        Parses ``headers`` into a :class:`RateLimitSnapshot`; if the response
        carried no recognized rate-limit header the parse returns ``None`` and
        this is a silent no-op. Otherwise the snapshot replaces any prior one for
        the same endpoint (last-write-wins).
        """
        try:
            snapshot = RateLimitSnapshot.from_headers(provider, model, headers)
        except Exception:  # noqa: BLE001 — telemetry must never break a live call
            return
        if snapshot is not None:
            self._snapshots[(provider, model)] = snapshot

    def observe(self, snapshot: RateLimitSnapshot) -> None:
        """Record an already-built snapshot (last-write-wins on its endpoint key)."""
        self._snapshots[(snapshot.provider, snapshot.model)] = snapshot

    def get(self, provider: str, model: str) -> Optional[RateLimitSnapshot]:
        """The latest snapshot for one endpoint, or ``None`` if never observed."""
        return self._snapshots.get((provider, model))

    def snapshots(self) -> List[RateLimitSnapshot]:
        """Every current snapshot, sorted by ``(provider, model)`` for stable output."""
        return [self._snapshots[k] for k in sorted(self._snapshots)]

    def is_empty(self) -> bool:
        """True when nothing has been observed yet."""
        return not self._snapshots


__all__ = ["RateLimitTracker"]
