#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``cli.serving`` — the multi-session host spine (§Phase 0, the enabling layer).

``build_app`` (``cli/app.py``) is **single-session**: one stdin port + one
``driver.run()`` while-loop over one resident role. A network host (AG-UI SSE,
ACP stdio, later 飞书/web) is **multi-session**: one process concurrently serves
N connections / threads, each needing its own ``{port, consumer, projector}``
while sharing the one engine (``config`` + ``context`` + ``role_factory``).

This package is the thin orchestration layer that sits **above** the
:class:`~mote.product.interaction.driver.SessionDriver`, adding only that multiplexing — it
imports no transport, touches no core engine class (only the ``backend`` seam),
and reuses the exact same construction path (:func:`~mote.product.entrypoints.cli.bootstrap.build_engine`).
The two pieces:

* :class:`SessionRegistry` — the resident ``session_id → {control, role}`` map
  (a thread/session lives across many turns), minting sessions from the shared
  ``EngineBuild`` so single- and multi-session hosts can never drift.
* :class:`ConnectionScope` — the per-connection / per-turn ``{port, consumer,
  BaseProjector}`` bundle: subscribe to the session's Role Telemetry, drive one turn,
  unsubscribe, ``aclose``. It is the multi-session dual of ``driver.run()``'s
  single-session loop.
"""

from __future__ import annotations

from mote.product.session_hosting.connection import ConnectionScope
from mote.product.session_hosting.prompt_broker import PromptBroker
from mote.product.session_hosting.registry import ResidentSession, SessionRegistry

__all__ = ["SessionRegistry", "ResidentSession", "ConnectionScope", "PromptBroker"]
