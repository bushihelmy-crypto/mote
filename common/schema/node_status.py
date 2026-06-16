"""Unified status enum for background tasks and graph nodes.

This is a LEAF module (stdlib only), safe to import from anywhere without
risking circular imports.
"""

from __future__ import annotations

from enum import Enum


class BgStatus(str, Enum):
    """Unified status values for background tasks and graph nodes."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    WAITING_FOR_ROUTE = "waiting_for_route"


