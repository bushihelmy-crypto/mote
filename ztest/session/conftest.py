"""Shared fixtures for the session test suite.

Session writes now flow through the process-wide :class:`DiskWriter` singleton.
The codebase's tests run many ``asyncio.run`` calls, each on a fresh loop, so a
singleton worker bound to one test's loop would be orphaned in the next. Give
each test a fresh writer and flush any leftover queued jobs inline at teardown
so nothing leaks across tests.
"""
from __future__ import annotations

import pytest

from mote.common.disk import writer


@pytest.fixture(autouse=True)
def _isolate_disk_writer():
    writer.set_disk_writer(None)
    yield
    leftover = writer._writer
    if leftover is not None:
        try:
            leftover.flush_inline()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
    writer.set_disk_writer(None)
