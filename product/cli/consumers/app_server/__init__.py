#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Machine host: codex app-server protocol (STUB — §6 / §8.1 phase④).

The machine-side counterpart of the terminal consumer. It consumes the **machine**
protocol (``ServerNotification`` from ``proto/``, NOT ``ViewEvent``) and writes
newline-delimited JSON notifications on stdio, letting an orchestrator (Symphony)
drive Mote as a codex app-server (ARCHITECTURE §6).

This is a documented stub: the machine protocol (``mote.product.cli/proto/``) and its
``AppServerProjector`` (the ``ViewProjector`` twin) are filled in phase④. The
key §6.1 finding — thread/turn/session already exist in ``environment/control.py``
and ``turn_id``/``session_id`` already ride on the events — means this is a thin
adapter layer, not a core change.

To implement: ``proto/projector.py`` (AppServerProjector), ``proto/schema.py``
(field-aligned with codex), ``proto/rpc.py`` (stdio transport + method routing),
and ``app_server/consumer.py`` here.
"""

__all__: list = []
