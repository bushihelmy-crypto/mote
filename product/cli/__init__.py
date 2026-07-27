#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mote.product.cli — the decoupled, multi-consumer display framework for Mote.

This package is the *target architecture* described in ``ARCHITECTURE.md``: the
core only emits "what happened" through per-Role telemetry, two
projectors fold that single source of truth into "what a human should see"
(``ViewEvent``) and "what a machine should receive" (``ServerNotification``), and
any number of consumers each decide "how to deliver".

Phase ① (this slice) wires the **terminal** path end-to-end:

    AgentEvent ─▶ ViewProjector ─▶ ViewEvent ─▶ TerminalConsumer (rich TUI)

while leaving the machine protocol (``proto/``) and multi-tenant gateway
(``router/``) as documented stubs to be filled in later phases.

It builds *on top of* — and never modifies — ``mote.runtime.events`` (the
Telemetry) and ``mote.orchestration.environment.control`` (the control plane).

Construction lives in :mod:`mote.product.cli.app`. Keeping the package root
side-effect free prevents importing a CLI catalog from recursively constructing
the full Application composition root.
"""

__all__: list[str] = []
