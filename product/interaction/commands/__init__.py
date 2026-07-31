#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Slash commands — immutable definitions and per-Application catalogs (§2.7).

``registry`` provides :class:`Command` / :class:`CommandRegistry` +
``@register_command``; ``default_registry()`` builds an isolated catalog from
the stock definitions.
"""

from __future__ import annotations

from mote.product.interaction.commands.catalog import Command, CommandRegistry, default_registry, register_command

__all__ = [
    "Command",
    "CommandRegistry",
    "register_command",
    "default_registry",
]
