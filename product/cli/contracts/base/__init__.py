#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.product.cli.contracts.base`` — subclassable base classes shared across hosts.

Distinct from :mod:`mote.product.cli.contracts.interface` (PEP 544 Protocols, structural):
these are concrete bases meant to be **subclassed** or composed by every host.

* :class:`BaseConsumer` — eat/dispatch plumbing behind the ``Consumer`` contract.
* :class:`SinkConsumer` — a ``BaseConsumer`` that folds events into wire payloads
  and pushes them to an injected async ``sink`` (shared by every network consumer).
* :class:`BaseProjector` — fans one injected ``AgentEvent`` fold out to many
  consumers via per-consumer capability adapters.
"""

from mote.product.cli.contracts.base.consumer import BaseConsumer, Sink, SinkConsumer
from mote.product.cli.contracts.base.projector import BaseProjector

__all__ = ["BaseConsumer", "SinkConsumer", "Sink", "BaseProjector"]
