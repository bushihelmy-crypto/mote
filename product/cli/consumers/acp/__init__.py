#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ACP transport — the stdio JSON-RPC host for a Zed-style editor client.

The output half (:class:`AcpConsumer`) folds mote's neutral ``ViewEvent`` stream
into ACP ``session/update`` payloads (via the pure :mod:`mote.product.cli.consumers._wire.acp`
mapper) and hands each to a sink; the input half (:class:`AcpPort`) carries a
prompt's turn upstream and gates a tool call via an inline
``session/request_permission`` round-trip. :class:`AcpServer` binds them onto one
bidirectional JSON-RPC link over ``stdin``/``stdout``, driving one turn per
``session/prompt`` request against a resident session from a shared
:class:`~mote.product.cli.serving.SessionRegistry`.

A pure *transport* layer: every wire-shape decision lives in the ``_wire/acp``
mapper (unit-testable without a pipe), every multi-session concern in
``cli/serving`` (``SessionRegistry`` + ``ConnectionScope``). The package here just
moves bytes over stdio.
"""

from __future__ import annotations

from mote.product.cli.consumers.acp.consumer import AcpConsumer, build_acp_consumer
from mote.product.cli.consumers.acp.port import AcpPort
from mote.product.cli.consumers.acp.server import AcpServer, serve

__all__ = ["AcpConsumer", "build_acp_consumer", "AcpPort", "AcpServer", "serve"]
