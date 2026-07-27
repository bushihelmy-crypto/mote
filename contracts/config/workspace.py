#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Workspace disk-layer settings (currently: the periodic TTL cleanup sweep)."""
from __future__ import annotations

from pydantic import Field

from mote.contracts.config.base import ConfigModel as YamlModel


class WorkspaceCleanupConfig(YamlModel):
    """Time-based cleanup of the per-session workspace tree.

    The session's ``rollout.jsonl`` mtime is the single liveness signal, read
    against two thresholds. A value of ``0`` (or any ``<= 0``) disables that
    tier — the supported "never clean" form. The whole sweep can also be turned
    off with ``enabled: false``. It runs at most once per 24h at session start.
    """

    # Master switch for the sweep. Off = the workspace grows unbounded.
    enabled: bool = True

    # Days of rollout inactivity before a session's *entire* directory (rollout
    # + blobs + artifacts) is removed. 0 = never expire whole sessions.
    session_ttl_days: int = 30

    # Days of rollout inactivity before a still-alive session sheds its bulky
    # overflow artifacts (tool_results/ + task_outputs/), keeping rollout +
    # blobs. Kept shorter than the session TTL. 0 = never shed artifacts.
    artifact_ttl_days: int = 7


class WorkspaceConfig(YamlModel):
    """Settings for the on-disk workspace (``~/.mote/workspace`` by default)."""

    cleanup: WorkspaceCleanupConfig = Field(default_factory=WorkspaceCleanupConfig)
