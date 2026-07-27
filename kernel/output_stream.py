"""Run-scoped provisional structured-output snapshots."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from mote.contracts.events.types import OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.kernel.telemetry import emit_event_sync


class OutputSnapshotAccumulator:
    def __init__(self, *, run_id: str, schema_fingerprint: str) -> None:
        self.run_id = run_id
        self.schema_fingerprint = schema_fingerprint
        self._buffer = ""
        self._revision = 0
        self._current_key: str | None = None

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self._buffer += chunk
        try:
            value = json.loads(self._buffer)
        except (TypeError, ValueError):
            self._invalidate("stream_changed")
            return
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key == self._current_key:
            return
        self._invalidate("superseded")
        self._revision += 1
        self._current_key = key
        emit_event_sync(
            OutputSnapshotEvent(
                run_id=self.run_id,
                revision=self._revision,
                schema_fingerprint=self.schema_fingerprint,
                value=value,
            )
        )

    def invalidate(self, reason: str) -> None:
        self._invalidate(reason)

    def _invalidate(self, reason: str) -> None:
        if self._current_key is None:
            return
        emit_event_sync(
            OutputSnapshotInvalidatedEvent(
                run_id=self.run_id,
                revision=self._revision,
                reason=reason,
            )
        )
        self._current_key = None


_active: ContextVar[OutputSnapshotAccumulator | None] = ContextVar("mote_output_snapshot_accumulator", default=None)


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
