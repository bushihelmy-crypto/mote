#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mote.cli — the decoupled, multi-consumer display framework for Mote.

This package is the *target architecture* described in ``ARCHITECTURE.md``: the
core only emits "what happened" (``AgentEvent`` on a per-role ``EventBus``), two
projectors fold that single source of truth into "what a human should see"
(``ViewEvent``) and "what a machine should receive" (``ServerNotification``), and
any number of consumers each decide "how to deliver".

Phase ① (this slice) wires the **terminal** path end-to-end:

    AgentEvent ─▶ ViewProjector ─▶ ViewEvent ─▶ TerminalConsumer (rich TUI)

while leaving the machine protocol (``proto/``) and multi-tenant gateway
(``router/``) as documented stubs to be filled in later phases.

It builds *on top of* — and never modifies — ``mote.common.events`` (the
event spine) and ``mote.environment.control`` (the control plane).
"""

__all__ = ["build_app", "run_app"]


def __getattr__(name: str):  # PEP 562 — lazy top-level re-export.
    # ``build_app`` / ``run_app`` pull in the whole app stack (consumers, io,
    # driver, the framework runtime). Re-export them lazily so that merely
    # importing a leaf subpackage (e.g. ``mote.cli.view``) never forces that
    # heavy subtree to import — and so each layer stays independently importable.
    if name in ("build_app", "run_app"):
        from mote.cli.app import build_app, run_app

        return {"build_app": build_app, "run_app": run_app}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
