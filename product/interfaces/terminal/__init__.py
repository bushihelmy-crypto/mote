#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Terminal host: the rich TUI consumer of the human ``ViewEvent`` protocol."""

from mote.product.interfaces.terminal.consumer import PlainTerminalConsumer, build_terminal_consumer

__all__ = ["PlainTerminalConsumer", "build_terminal_consumer"]
