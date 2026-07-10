#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``DeliveryManager`` — outbound delivery reliability for gateways (§7.3).

A documented phase-② stub. ``Consumer.handle(ev)`` is fire-and-forget; public
platforms impose strict rate limits, may fail / need retry, and emit async
receipts. The ``EventBus`` mirror/durable layer only guarantees *core → projector*
— it cannot reach *consumer → external network*. ``DeliveryManager`` adds that
missing outbound tier: token-bucket rate limiting, exponential-backoff retry that
distinguishes retryable (429 / 5xx) from terminal (permission / ban) failures,
delivery receipts, and backpressure to avoid unbounded queueing.

Method bodies raise :class:`NotImplementedError`; the contract is fixed by §7.3,
the implementation lands later. Import is side-effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class DeliveryReceipt:
    """Outcome of one outbound send (§7.3): platform message id + status."""

    ok: bool
    target: Any = None
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False


class DeliveryManager:
    """Outbound reliability layer between a consumer and an external platform.

    Rate limiting (token bucket) · exponential-backoff retry (429/5xx retryable vs
    permission/ban terminal) · delivery receipts · backpressure.
    """

    async def send(self, target: Any, payload: Any) -> DeliveryReceipt:
        """Deliver *payload* to *target* with rate limiting + retry; return a receipt."""
        raise NotImplementedError("DeliveryManager.send is a phase-② stub (§7.3)")


__all__ = ["DeliveryManager", "DeliveryReceipt"]
