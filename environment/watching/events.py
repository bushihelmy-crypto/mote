#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""File-change event schema for the watcher subsystem.

A :class:`FileChangeEvent` is what :class:`~metagpt.environment.watching.watcher.FileWatcher`
emits when a watched path's ``(mtime, size)`` differs between two polls. It is a
plain value object — the watcher core stays agnostic about who consumes it (the
hook glue layer turns each one into a ``FileChanged`` hook fire).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Change-type discriminators.
CREATED = "created"
MODIFIED = "modified"
DELETED = "deleted"


@dataclass(frozen=True)
class FileChangeEvent:
    """A single detected change to a watched file.

    ``mtime``/``size`` describe the file *after* the change (both ``0`` for a
    ``deleted`` event, since the file is gone).
    """

    path: str
    change_type: str  # one of: created | modified | deleted
    mtime: float = 0.0
    size: int = 0


__all__ = ["FileChangeEvent", "CREATED", "MODIFIED", "DELETED"]
