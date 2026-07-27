#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Feishu / Lark host (STUB — §8.1 phase⑤).

A non-streaming IM consumer: capabilities ``streaming=False, markdown=True``. The
upstream :class:`CapabilityAdapter` therefore buffers ``MessageBlockDelta`` and
hands this consumer a single ``MessageBlockCompleted`` per block, which it posts
as one Lark card (ARCHITECTURE §2.4 / §4.2) — the canonical demonstration that
one ``ViewEvent`` stream feeds both a live TUI and a batch chat card with no
``if consumer == "feishu"`` branch anywhere.

To implement: add ``feishu/consumer.py`` with a ``FeishuConsumer(BaseConsumer)``
folding ``ToolCall*`` into the card's "execution" block + a webhook sender, and
``@register_consumer("feishu", capabilities=FEISHU_CAPS, validate=...)``.
"""

__all__: list = []
