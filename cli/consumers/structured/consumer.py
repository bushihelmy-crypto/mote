#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``StructuredConsumer`` — emit each ``ViewEvent`` as one JSON line.

The §8.1 phase③ litmus test: if the decoupling is real, a *headless* consumer
that just serializes the ``ViewEvent`` stream (à la ``codex exec --json``) drops
in with zero core changes — it shares the exact same projected protocol the rich
terminal consumes, only delivering it as newline-delimited JSON instead of panels.

It declares ``streaming=True`` (``STRUCTURED_CAPS``): a structured sink wants
every event verbatim (it does its own downstream shaping), so the upstream
:class:`CapabilityAdapter` must *not* buffer deltas into completed blocks.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mote.cli.contracts.base import BaseConsumer
from mote.cli.contracts.view import STRUCTURED_CAPS, Capabilities


class StructuredConsumer(BaseConsumer):
    """Serialize each ``ViewEvent`` to one JSON line on an output stream."""

    capabilities: Capabilities = STRUCTURED_CAPS

    def __init__(self, out=None):
        self._out = out if out is not None else sys.stdout

    def on_unhandled(self, ev: Any) -> None:
        """Serialize every kind identically (no per-kind methods → all land here).

        Because ``StructuredConsumer`` defines no ``on_<kind>`` methods, both the
        async (``handle``) and sync (``handle_sync``) dispatch paths route every
        event here — so a stream delta (sync) and a tool result (async) are both
        emitted as one JSON line, in arrival order.
        """
        payload = {"kind": getattr(ev, "kind", "view_event")}
        # pydantic BaseModel -> dict; ``kind`` is a ClassVar so model_dump omits it.
        try:
            payload.update(ev.model_dump())
        except Exception:  # noqa: BLE001 — fall back to a best-effort vars() dump
            payload.update({k: v for k, v in vars(ev).items() if not k.startswith("_")})
        self._out.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        flush = getattr(self._out, "flush", None)
        if flush is not None:
            flush()


def build_structured_consumer(config: Any = None) -> StructuredConsumer:
    return StructuredConsumer()


# Self-register on import.
try:
    from mote.cli.consumers.registry import register_consumer

    register_consumer("structured", capabilities=STRUCTURED_CAPS)(build_structured_consumer)
except Exception:  # noqa: BLE001
    pass


__all__ = ["StructuredConsumer", "build_structured_consumer"]
