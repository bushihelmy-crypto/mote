"""Stable contracts for interactive terminal surfaces."""
from __future__ import annotations

from pydantic import BaseModel, Field

TERMINAL_MEDIA_TYPE = "text/x-terminal"
TERMINAL_FRAME_BASE_SEQUENCE = "base_sequence"
TERMINAL_FRAME_COLS = "cols"
TERMINAL_FRAME_MODE = "mode"
TERMINAL_FRAME_MODE_DELTA = "delta"
TERMINAL_FRAME_MODE_FULL = "full"
TERMINAL_FRAME_ROWS = "rows"


class TerminalResizeInput(BaseModel):
    """A bounded PTY character-grid resize requested by a terminal frontend."""

    cols: int = Field(ge=20, le=1000)
    rows: int = Field(ge=5, le=500)


__all__ = [
    "TERMINAL_FRAME_BASE_SEQUENCE",
    "TERMINAL_FRAME_COLS",
    "TERMINAL_FRAME_MODE",
    "TERMINAL_FRAME_MODE_DELTA",
    "TERMINAL_FRAME_MODE_FULL",
    "TERMINAL_FRAME_ROWS",
    "TERMINAL_MEDIA_TYPE",
    "TerminalResizeInput",
]
