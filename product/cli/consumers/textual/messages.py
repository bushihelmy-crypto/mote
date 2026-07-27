"""Textual message types shared by the UI host and its event consumer."""
from __future__ import annotations

from typing import Any

from textual.message import Message


class ViewEventMessage(Message):
    """Carry one projected event onto the Textual app message pump."""

    def __init__(self, event: Any) -> None:
        super().__init__()
        self.event = event


__all__ = ["ViewEventMessage"]
