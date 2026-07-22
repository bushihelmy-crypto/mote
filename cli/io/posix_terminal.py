"""POSIX raw-terminal adapter isolated from the host-neutral terminal port."""
from __future__ import annotations

import termios
import tty
from typing import Callable, TextIO


def enter_raw(stream: TextIO) -> Callable[[], None]:
    """Put *stream* into raw mode and return an idempotent best-effort restore."""
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)

    def restore() -> None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:  # noqa: BLE001 — terminal may already be closed
            pass

    return restore


__all__ = ["enter_raw"]
