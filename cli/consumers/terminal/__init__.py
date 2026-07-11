#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Terminal host: the rich TUI consumer of the human ``ViewEvent`` protocol."""

from mote.cli.consumers.terminal.consumer import TerminalConsumer, build_terminal_consumer

__all__ = ["TerminalConsumer", "build_terminal_consumer"]
