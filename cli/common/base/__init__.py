#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``metagpt.cli.common.base`` — subclassable base classes shared across hosts.

Distinct from :mod:`metagpt.cli.common.interface` (PEP 544 Protocols, structural):
these are concrete bases meant to be **subclassed** or composed by every host.

* :class:`BaseConsumer` — eat/dispatch plumbing behind the ``Consumer`` contract.
* :class:`BaseProjector` — fans one injected ``AgentEvent`` fold out to many
  consumers via per-consumer capability adapters.
"""

from metagpt.cli.common.base.consumer import BaseConsumer
from metagpt.cli.common.base.projector import BaseProjector

__all__ = ["BaseConsumer", "BaseProjector"]
