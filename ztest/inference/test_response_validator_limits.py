import asyncio

import pytest

from mote.product.models.transports.validator import PrecommitLimitExceeded, PrecommitResponseGuard


class Lines:
    def __init__(self, lines, *, delay=0.0):
        self._lines = iter(lines)
        self._delay = delay

    async def readline(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return next(self._lines, b"")


def test_precommit_guard_rejects_byte_frame_and_time_limits_before_commit():
    async def scenario():
        byte_guard = PrecommitResponseGuard(max_bytes=3, max_frames=8, max_seconds=1)
        with pytest.raises(PrecommitLimitExceeded, match="byte limit"):
            await byte_guard.readline(Lines((b"four",)))

        frame_guard = PrecommitResponseGuard(max_bytes=8, max_frames=1, max_seconds=1)
        await frame_guard.readline(Lines((b"a",)))
        with pytest.raises(PrecommitLimitExceeded, match="frame limit"):
            await frame_guard.readline(Lines((b"b",)))

        time_guard = PrecommitResponseGuard(max_bytes=8, max_frames=8, max_seconds=0.001)
        with pytest.raises(PrecommitLimitExceeded, match="time limit"):
            await time_guard.readline(Lines((b"a",), delay=0.01))

    asyncio.run(scenario())


def test_precommit_guard_stops_charging_after_semantic_commit():
    async def scenario():
        guard = PrecommitResponseGuard(max_bytes=1, max_frames=1, max_seconds=1)
        await guard.readline(Lines((b"a",)))
        guard.commit()
        assert await guard.readline(Lines((b"unbounded-after-commit",)))

    asyncio.run(scenario())
