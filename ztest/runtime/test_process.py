from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from mote.runtime.process import EXEC_TIMEOUT_EXIT_CODE, aexecute


@pytest.mark.asyncio
async def test_fast_output_process_does_not_stall_after_exit() -> None:
    payload_size = 50_000
    script = f"import sys; sys.stdout.write('x' * {payload_size})"
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    for _ in range(10):
        result = await asyncio.wait_for(aexecute(command, wait=True, timeout=1.0), timeout=2.0)

        assert result[0] == 0
        assert len(result[1]) == payload_size
        assert result[2] == ""


@pytest.mark.asyncio
async def test_partial_timeout_terminates_process_and_keeps_output() -> None:
    script = "import sys, time; print('started', flush=True); time.sleep(10)"
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    code, stdout, stderr, timed_out = await aexecute(
        command,
        wait=True,
        timeout=0.05,
        return_partial_on_timeout=True,
    )

    assert code == EXEC_TIMEOUT_EXIT_CODE
    assert stdout == "started"
    assert stderr == ""
    assert timed_out is True
