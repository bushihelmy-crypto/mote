#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``AcpConsumer`` — ViewEvent → ACP ``session/update`` notifications (output half).

A :class:`BaseConsumer` that folds each projected ``ViewEvent`` through the pure
:mod:`mote.cli.consumers._wire.acp` mapper and hands each resulting ``update``
dict to an injected async ``sink`` (the server binds this to a ``session/update``
JSON-RPC notification writer). The consumer owns NO transport — it only produces
``update`` payloads and calls the sink — so it is fully unit-testable with a
list-appending fake sink.

One :class:`AcpWireState` lives per consumer instance (one per resident ACP
session), minting the stable ``messageId`` / correlating ``toolCallId`` the ACP
stream needs. Unlike AG-UI (one state per SSE run), an ACP connection is
long-lived across many ``session/prompt`` turns, so the server reuses one
consumer + state for a session's lifetime.

Declares ``streaming=True`` (+ markdown / interactive): ACP is a token-streaming
protocol (``agent_message_chunk`` deltas), so the upstream ``CapabilityAdapter``
must pass deltas through untouched rather than buffering into completed blocks.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from mote.cli.consumers._wire import acp
from mote.cli.contracts.base import Sink, SinkConsumer
from mote.cli.contracts.view import Capabilities

# An editor client: streams tokens, renders markdown, gates tool calls via the
# permission request. ``images`` off — ACP image content needs inline base64 we
# degrade to text pointers (see ``_wire/acp._on_media``).
ACP_CAPS = Capabilities(
    streaming=True,
    markdown=True,
    syntax_highlight=False,
    interactive=True,
    rich_panels=False,
    images=False,
)


class AcpConsumer(SinkConsumer):
    """Fold ``ViewEvent``s into ACP ``update`` dicts and push them to an async sink.

    ``sink`` is the transport's ``session/update`` writer (``async
    def(update_dict) -> None``); the consumer never touches stdio itself.
    ``session_id`` seeds the per-session correlation state the mapper stamps onto
    every message id / tool id. All the sink/emit/close plumbing lives in
    :class:`SinkConsumer`; this class supplies only the pure ACP fold.
    """

    capabilities: Capabilities = ACP_CAPS
    _log_label = "AcpConsumer"

    def __init__(self, *, session_id: str, sink: Optional[Sink] = None) -> None:
        super().__init__(sink)
        self._state = acp.AcpWireState(session_id=session_id)

    @property
    def wire_state(self) -> acp.AcpWireState:
        return self._state

    def _fold(self, ev: Any) -> Iterable[Dict[str, Any]]:
        return acp.to_acp_updates(ev, self._state)


def build_acp_consumer(config: Any = None) -> AcpConsumer:
    """Registry builder — a bare consumer (server rebinds session/sink per connection).

    ``build_consumers`` constructs one at app-wire time with a placeholder id; the
    ACP server does not use this path (it constructs a fresh consumer per session
    with the real ``sessionId``). Present so ``acp`` is a first-class registered
    channel name for diagnostics/help.
    """
    return AcpConsumer(session_id="pending")


# Self-register on import (mirrors the AG-UI consumer's pattern).
try:
    from mote.cli.consumers.registry import register_consumer

    register_consumer("acp", capabilities=ACP_CAPS)(build_acp_consumer)
except Exception:  # noqa: BLE001
    pass


__all__ = ["AcpConsumer", "build_acp_consumer", "ACP_CAPS", "Sink"]
