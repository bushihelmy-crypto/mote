#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``Projector`` — the pure ``AgentEvent → list[ViewEvent]`` fold contract.

The narrow slice :class:`metagpt.cli.common.base.projector.BaseProjector` relies
on: anything with a ``project(event) -> list`` method conforms. This decouples
the reusable fan-out plumbing (``BaseProjector``, in ``common``) from any
*concrete* host fold (e.g. ``ViewProjector``, which stays host-side in
:mod:`metagpt.cli.view`), so the base never imports upward.

This is a LEAF interface module: it imports only ``typing``, so it can be
imported from any host without risking a cycle.
"""

from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable


@runtime_checkable
class Projector(Protocol):
    """Folds one ``AgentEvent`` into zero-or-more projected events (pure)."""

    def project(self, event: Any) -> List[Any]:
        """Return the projected events for one spine event (no I/O)."""
        ...


__all__ = ["Projector"]
