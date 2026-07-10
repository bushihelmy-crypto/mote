#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Structural interfaces (PEP 544 Protocols) for the display framework's hosts.

Each Protocol describes the *narrow slice* a host must satisfy, so anything
shaped right conforms — no inheritance required. This is a LEAF package: every
module imports only stdlib ``typing``, so it can be imported from any host
(terminal / Web / IM / machine) without risking a cycle.
"""

from metagpt.cli.common.interface.consumer import Consumer
from metagpt.cli.common.interface.ports import (
    BroadcastPort,
    InputPort,
    InteractivePort,
    ProtocolPort,
)
from metagpt.cli.common.interface.projector import Projector

__all__ = [
    "InputPort",
    "InteractivePort",
    "BroadcastPort",
    "ProtocolPort",
    "Consumer",
    "Projector",
]
