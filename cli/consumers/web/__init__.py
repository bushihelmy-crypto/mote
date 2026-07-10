#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Web host (STUB — §8.1 phase⑤).

An SSE/WebSocket consumer of the human ``ViewEvent`` protocol. Capabilities:
``streaming=True, markdown=True`` (a browser renders markdown and can show live
deltas), so it reuses the rich stream almost verbatim — the only consumer-specific
work is the transport (push each ``ViewEvent`` as an SSE frame / WS message).

To implement: add ``web/consumer.py`` with a ``WebConsumer(BaseConsumer)`` and
``@register_consumer("web", capabilities=WEB_CAPS)``; no core changes.
"""

__all__: list = []
