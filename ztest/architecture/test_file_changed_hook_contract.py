from __future__ import annotations

import pytest

from mote.contracts.events.file.observation import FileChangedEvent
from mote.contracts.file.identity import (
    AbsentVersion,
    FileChangeAttribution,
    FileChangeKind,
    NameIdentity,
    PresentVersion,
    TargetIdentity,
)
from mote.contracts.hook import FileChangedInvocation, FileChangedPayload, HookIdentity
from mote.runtime.hook.manager import HookManager
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.hook.wire import HookWireSerializer


def _versions():
    name = NameIdentity("entry", "test")
    return AbsentVersion(name), PresentVersion(
        name_identity=name,
        target_identity=TargetIdentity("target", "test"),
        size=3,
        mtime_ns=4,
        digest="a" * 64,
        metadata_digest="b" * 64,
    )


def test_file_changed_wire_round_trip_is_strict() -> None:
    prior, current = _versions()
    payload = FileChangedPayload(
        "/project/a.py",
        FileChangeKind.CREATED,
        prior,
        current,
        FileChangeAttribution.EXTERNAL,
    )
    codec = HookWireSerializer()
    wire = codec.file_changed_payload_to_json_dict(payload)
    assert codec.file_changed_payload_from_json_dict(wire) == payload
    with pytest.raises(ValueError, match="unknown"):
        codec.file_changed_payload_from_json_dict({**wire, "change_type": "renamed"})
    with pytest.raises(ValueError, match="canonical"):
        codec.file_changed_payload_from_json_dict({**wire, "version": {**wire["version"], "extra": True}})


@pytest.mark.asyncio
async def test_subscriber_preserves_canonical_file_types() -> None:
    prior, current = _versions()
    seen: list[FileChangedInvocation] = []
    manager = HookManager()
    manager.register("FileChanged", seen.append)
    await HookSubscriber(manager).handle(
        FileChangedEvent(
            "/project/a.py",
            FileChangeKind.CREATED,
            prior,
            current,
            FileChangeAttribution.MANAGED,
        )
    )
    assert seen[0].payload.change_type is FileChangeKind.CREATED
    assert seen[0].payload.prior_version is prior
    assert seen[0].payload.version is current
    assert seen[0].payload.attribution is FileChangeAttribution.MANAGED


def test_serializer_uses_file_codec_at_external_boundary() -> None:
    prior, current = _versions()
    invocation = FileChangedInvocation(
        identity=HookIdentity("session", "/project", ""),
        payload=FileChangedPayload(
            "/project/a.py",
            FileChangeKind.CREATED,
            prior,
            current,
            FileChangeAttribution.EXTERNAL,
        ),
    )
    wire = HookWireSerializer().to_json_dict(invocation)
    assert wire["change_type"] == "created"
    assert wire["prior_version"]["kind"] == "absent"
    assert wire["version"]["kind"] == "present"
