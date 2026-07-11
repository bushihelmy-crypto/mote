#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Slash commands — objectified + self-registering (§2.7).

``registry`` provides :class:`Command` / :class:`CommandRegistry` +
``@register_command``; ``builtin`` registers the stock commands on import.
``default_registry()`` returns the populated registry the driver dispatches on.
"""

from __future__ import annotations

from mote.cli.commands.registry import Command, CommandRegistry, default_registry, register_command

__all__ = [
    "Command",
    "CommandRegistry",
    "register_command",
    "default_registry",
]
