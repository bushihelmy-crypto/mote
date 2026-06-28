#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LLM stream emission.

Streamed LLM tokens are emitted onto the unified agent event bus as
:class:`~metagpt.common.events.types.LLMStreamDeltaEvent` observation events.
Whoever wants to mirror them live (the REPL renderer) or forward them (the web
reporter) subscribes to the bus — there is no process-global sink anymore, so
screen and disk can no longer diverge.

``log_llm_stream`` stays a plain sync function (the LLM providers call it from
inside their ``async for`` chunk loops) and uses the bus's sync fire-and-forget
delivery: it no-ops when no bus is bound (standalone client use, tests without a
bus), so the router never has to know who, if anyone, is listening.

This lives in the ``events`` package (not ``logs``) because it is purely an
event-bus concern: it depends only on ``events`` primitives. Keeping it here lets
``logs`` stay a pure leaf, so the dependency edge runs one-way ``events → logs``
(the bus uses ``logger``) with no cycle back.
"""

from __future__ import annotations

from metagpt.common.events.context import observe_event_sync
from metagpt.common.events.types import LLMStreamDeltaEvent


def log_llm_stream(msg):
    """Emit one streamed LLM token/chunk onto the active event bus (if any)."""
    observe_event_sync(LLMStreamDeltaEvent(token=msg))
