"""Runtime binding between model stream callbacks and one output accumulator."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mote.kernel.output.snapshots import OutputSnapshotAccumulator

_active: ContextVar[OutputSnapshotAccumulator | None] = ContextVar(
    "mote_runtime_output_snapshot_accumulator",
    default=None,
)


@contextmanager
def bind_output_snapshot_accumulator(
    accumulator: OutputSnapshotAccumulator | None,
) -> Iterator[None]:
    token = _active.set(accumulator)
    try:
        yield
    except BaseException:
        if accumulator is not None:
            accumulator.invalidate("stream_failed")
        raise
    finally:
        _active.reset(token)


def feed_output_stream(chunk: str) -> None:
    accumulator = _active.get()
    if accumulator is not None:
        accumulator.feed(chunk)


__all__ = ["bind_output_snapshot_accumulator", "feed_output_stream"]
