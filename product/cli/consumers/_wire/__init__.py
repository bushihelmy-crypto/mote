#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``_wire`` — pure ``ViewEvent → target-protocol`` mappers (no transport).

Each module here is a **transport-free** translation table from mote's neutral
:class:`~mote.product.cli.contracts.view.events.ViewEvent` spine to one external wire
protocol (AG-UI, ACP, ...). They import nothing about sockets / SSE / JSON-RPC —
only the ViewEvent union in — so they are trivially unit-testable and the single
place a protocol's shape lives. The transport layer (``consumers/<name>/``) owns
the socket and calls these to serialize.

This is the §2.4 "one spine, N transports" invariant made concrete: adding a
frontend never touches the projector or ViewEvent; it adds a mapper here + a thin
transport beside it.
"""

from __future__ import annotations

__all__: list[str] = []
