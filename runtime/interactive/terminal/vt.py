"""Canonical terminal VT state and recoverable incremental surface frames."""
from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass
from typing import Any

import pyte

from mote.contracts.surface import (
    TERMINAL_FRAME_BASE_SEQUENCE,
    TERMINAL_FRAME_COLS,
    TERMINAL_FRAME_MODE,
    TERMINAL_FRAME_MODE_DELTA,
    TERMINAL_FRAME_MODE_FULL,
    TERMINAL_FRAME_ROWS,
    TERMINAL_MEDIA_TYPE,
    SurfaceFrame,
)

DEFAULT_SCROLLBACK_LINES = 10_000
DEFAULT_DELTA_BYTES = 1024 * 1024

_NAMED_FOREGROUND = {
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "default": 39,
}
_NAMED_BACKGROUND = {
    "black": 40,
    "red": 41,
    "green": 42,
    "brown": 43,
    "blue": 44,
    "magenta": 45,
    "cyan": 46,
    "white": 47,
    "default": 49,
}


@dataclass(frozen=True, slots=True)
class _Delta:
    sequence: int
    content: str
    size: int


class TerminalVTState:
    """Own the terminal grid; raw PTY bytes are only incremental transport."""

    def __init__(
        self,
        cols: int,
        rows: int,
        *,
        scrollback_lines: int = DEFAULT_SCROLLBACK_LINES,
        delta_bytes: int = DEFAULT_DELTA_BYTES,
    ) -> None:
        if cols < 1 or rows < 1:
            raise ValueError("terminal VT dimensions must be positive")
        if scrollback_lines < 0 or delta_bytes < 0:
            raise ValueError("terminal VT bounds must be non-negative")
        self._screen = pyte.HistoryScreen(
            cols,
            rows,
            history=scrollback_lines,
        )
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._sequence = 0
        self._deltas: deque[_Delta] = deque()
        self._delta_bytes = delta_bytes
        self._retained_delta_bytes = 0

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def cols(self) -> int:
        return int(self._screen.columns)

    @property
    def rows(self) -> int:
        return int(self._screen.lines)

    @property
    def display(self) -> tuple[str, ...]:
        """Plain canonical rows, exposed for diagnostics and conformance tests."""
        return tuple(self._screen.display)

    def feed_bytes(self, data: bytes) -> None:
        if data:
            self.feed_text(self._decoder.decode(data, final=False))

    def feed_text(self, text: str) -> None:
        if not text:
            return
        self._stream.feed(text)
        self._sequence += 1
        size = len(text.encode("utf-8"))
        self._deltas.append(_Delta(self._sequence, text, size))
        self._retained_delta_bytes += size
        while self._deltas and self._retained_delta_bytes > self._delta_bytes:
            removed = self._deltas.popleft()
            self._retained_delta_bytes -= removed.size

    def resize(self, cols: int, rows: int) -> None:
        if cols < 1 or rows < 1:
            raise ValueError("terminal VT dimensions must be positive")
        if cols == self.cols and rows == self.rows:
            return
        self._screen.resize(lines=rows, columns=cols)
        self._sequence += 1
        self._deltas.clear()
        self._retained_delta_bytes = 0

    def full_frame(self) -> SurfaceFrame:
        return self._frame(
            self._render_full(),
            mode=TERMINAL_FRAME_MODE_FULL,
            base_sequence=-1,
        )

    def frame_after(self, after_sequence: int) -> SurfaceFrame:
        if after_sequence >= self._sequence:
            return self._frame(
                "",
                mode=TERMINAL_FRAME_MODE_DELTA,
                base_sequence=after_sequence,
            )
        deltas = [item for item in self._deltas if item.sequence > after_sequence]
        if deltas and deltas[0].sequence == after_sequence + 1:
            return self._frame(
                "".join(item.content for item in deltas),
                mode=TERMINAL_FRAME_MODE_DELTA,
                base_sequence=after_sequence,
            )
        return self.full_frame()

    def _frame(self, content: str, *, mode: str, base_sequence: int) -> SurfaceFrame:
        return SurfaceFrame(
            sequence=self._sequence,
            media_type=TERMINAL_MEDIA_TYPE,
            content=content,
            metadata=(
                (TERMINAL_FRAME_MODE, mode),
                (TERMINAL_FRAME_BASE_SEQUENCE, str(base_sequence)),
                (TERMINAL_FRAME_COLS, str(self.cols)),
                (TERMINAL_FRAME_ROWS, str(self.rows)),
            ),
        )

    def _render_full(self) -> str:
        history = list(self._screen.history.top)
        visible = [self._screen.buffer[row] for row in range(self.rows)]
        lines = history + visible
        rendered = ["\x1bc"]
        for index, line in enumerate(lines):
            rendered.append(self._render_line(line))
            if index + 1 < len(lines):
                rendered.append("\x1b[0m\r\n")
        rendered.append("\x1b[0m")
        rendered.append(f"\x1b[{int(self._screen.cursor.y) + 1};" f"{int(self._screen.cursor.x) + 1}H")
        rendered.append("\x1b[?25l" if self._screen.cursor.hidden else "\x1b[?25h")
        return "".join(rendered)

    def _render_line(self, line: Any) -> str:
        last = -1
        for column in range(self.cols):
            cell = line.get(column, self._screen.default_char)
            if cell.data != " " or cell.bg != "default":
                last = column
        if last < 0:
            return ""
        rendered: list[str] = []
        style = None
        for column in range(last + 1):
            cell = line.get(column, self._screen.default_char)
            next_style = self._style(cell)
            if next_style != style:
                rendered.append(next_style)
                style = next_style
            rendered.append(cell.data)
        return "".join(rendered)

    @staticmethod
    def _style(cell: Any) -> str:
        codes = ["0"]
        codes.append(str(_color_code(cell.fg, foreground=True)))
        codes.append(str(_color_code(cell.bg, foreground=False)))
        if cell.bold:
            codes.append("1")
        if cell.italics:
            codes.append("3")
        if cell.underscore:
            codes.append("4")
        if cell.blink:
            codes.append("5")
        if cell.reverse:
            codes.append("7")
        if cell.strikethrough:
            codes.append("9")
        return "\x1b[" + ";".join(codes) + "m"


def _color_code(color: str, *, foreground: bool) -> str | int:
    named = _NAMED_FOREGROUND if foreground else _NAMED_BACKGROUND
    if color in named:
        return named[color]
    if len(color) == 6:
        try:
            red = int(color[0:2], 16)
            green = int(color[2:4], 16)
            blue = int(color[4:6], 16)
        except ValueError:
            pass
        else:
            return f"{'38' if foreground else '48'};2;{red};{green};{blue}"
    return 39 if foreground else 49


__all__ = [
    "DEFAULT_DELTA_BYTES",
    "DEFAULT_SCROLLBACK_LINES",
    "TerminalVTState",
]
