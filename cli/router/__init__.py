#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``router`` — the multi-tenant gateway layer (公众平台层), a documented stub.

§1–§6 describe a **single-session** framework. Public platforms (WeChat MP,
Twitter, mailing lists) break that assumption: a flood of messages from thousands
of users must each map to an independent session. This package is the qualitative
step toward a *platform gateway* — adding routing **upstream** of the driver and
delivery reliability **downstream** of the consumers (ARCHITECTURE §7).

Phase ① ships this as a **documented stub**. Two pieces, both shape-fixed by §7:

* ``router/session_router.py`` — :class:`SessionRouter`: inbound → session routing
  with lazy driver creation. A pluggable ``key_fn`` maps a platform user
  (openid / handle / email-thread) to a routing key; the first message from a new
  key lazily spawns a :class:`SessionDriver`; idle sessions are evicted by the
  ``environment`` layer's existing LRU residency (reuse, don't rebuild); each key
  maps to a persisted ``session_id`` so a returning user can resume (§7.2).
* ``router/delivery.py`` — :class:`DeliveryManager`: outbound reliability between a
  consumer and an external platform — token-bucket rate limiting, exponential-
  backoff retry (429/5xx retryable vs permission/ban terminal), delivery receipts,
  and backpressure to avoid unbounded queueing (§7.3).

Platform-native shaping (Twitter thread-splitting at 280 chars, WeChat's 48-hour
push window) stays in each consumer (§7.4) — the router only normalizes inbound,
routes, and guarantees outbound; it never re-derives presentation.
"""

from __future__ import annotations

from mote.cli.router.delivery import DeliveryManager, DeliveryReceipt
from mote.cli.router.session_router import SessionRouter

__all__ = ["SessionRouter", "DeliveryManager", "DeliveryReceipt"]
