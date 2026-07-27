from __future__ import annotations

import pytest

from mote.contracts.ports import RuntimeCheckpointSink
from mote.contracts.runtimes import CheckpointFidelity, RuntimeCheckpoint
from mote.runtime.session import RuntimeCheckpointRecorder, SessionLog, SessionMetaEvent
from mote.runtime.session.replay import replay


def _checkpoint(revision: int) -> RuntimeCheckpoint:
    return RuntimeCheckpoint(
        runtime_id="terminal-1",
        kind="terminal",
        epoch=1,
        revision=revision,
        codec="terminal-state+json@1",
        schema_version=1,
        payload_ref=f"memory:{revision}",
        fidelity=CheckpointFidelity.LOGICAL,
    )


def test_runtime_checkpoint_recorder_satisfies_sink_protocol(tmp_path):
    recorder = RuntimeCheckpointRecorder(SessionLog("runtime-checkpoint-protocol", base_dir=str(tmp_path)))

    assert isinstance(recorder, RuntimeCheckpointSink)


@pytest.mark.asyncio
async def test_runtime_checkpoint_recorder_appends_replayable_last_state(tmp_path):
    log = SessionLog("runtime-checkpoint", base_dir=str(tmp_path))
    await log.append(SessionMetaEvent(session_id="runtime-checkpoint"))
    recorder = RuntimeCheckpointRecorder(log)

    await recorder.persist(_checkpoint(1), reason="write-commit")
    await recorder.persist(_checkpoint(2), reason="handoff-after")

    assert replay(log).runtime_checkpoints == {"terminal:default": _checkpoint(2)}
