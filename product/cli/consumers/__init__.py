#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``consumers`` — the host axis (axis three): how a projected event is delivered.

Each consumer adapts one *projected* protocol (``ViewEvent`` for humans,
``ServerNotification`` for machines) to one host (terminal / structured / web /
feishu / app-server). Builtins declare immutable metadata with
``@register_consumer``; each Application copies those definitions into its own
catalog.

Phase ① ships two working human channels — ``terminal`` (rich TUI) and
``structured`` (JSON-lines) — plus documented stubs for ``web`` / ``feishu`` /
``app_server`` to be filled in later phases (§8.1 ④⑤).
"""

from mote.product.cli.consumers.registry import (
    ConsumerSpec,
    build_consumer,
    build_consumers,
    register_consumer,
    registered_consumers,
)
from mote.product.cli.contracts.base import BaseConsumer
from mote.product.cli.contracts.interface import Consumer

__all__ = [
    "Consumer",
    "BaseConsumer",
    "ConsumerSpec",
    "register_consumer",
    "registered_consumers",
    "build_consumer",
    "build_consumers",
]
