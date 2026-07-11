#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.cli.contracts.base`` — subclassable base classes shared across hosts.

Distinct from :mod:`mote.cli.contracts.interface` (PEP 544 Protocols, structural):
these are concrete bases meant to be **subclassed** or composed by every host.

* :class:`BaseConsumer` — eat/dispatch plumbing behind the ``Consumer`` contract.
* :class:`BaseProjector` — fans one injected ``AgentEvent`` fold out to many
  consumers via per-consumer capability adapters.
"""

from mote.cli.contracts.base.consumer import BaseConsumer
from mote.cli.contracts.base.projector import BaseProjector

__all__ = ["BaseConsumer", "BaseProjector"]
