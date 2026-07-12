#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Input ports — the inbound axis (§2.5).

``ports`` defines the three inbound semantics (Interactive / Broadcast / Protocol)
over one shared :class:`InputPort` base. ``terminal_io`` provides the terminal's
:class:`InteractivePort` (stdin + two-stage SIGINT). ``human_channel`` adapts a
Role's ``ask_user`` onto any port's ``ask``.
"""

from __future__ import annotations

from mote.cli.contracts.interface.ports import BroadcastPort, InputPort, InteractivePort, ProtocolPort
from mote.cli.io.human_channel import PortHumanChannel
from mote.cli.io.terminal_io import TerminalPort

__all__ = [
    "InputPort",
    "InteractivePort",
    "BroadcastPort",
    "ProtocolPort",
    "TerminalPort",
    "PortHumanChannel",
]
