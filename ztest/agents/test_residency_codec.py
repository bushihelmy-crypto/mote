from __future__ import annotations

import json

import pytest

from mote.contracts.content import ContentDigest
from mote.orchestration.agents.residency.codec import decode_residency_record, encode_residency_record
from mote.orchestration.agents.residency.model import ResidencyFence, ResidencyIdentity, ResidencyRecord

DIGEST = ContentDigest("a" * 64)


def _record() -> ResidencyRecord:
    return ResidencyRecord(
        identity=ResidencyIdentity(
            logical_agent_id="agent-1",
            root_agent_id="root-1",
            parent_agent_id="parent-1",
            agent_path="/root/worker",
            nickname="worker",
            definition_id="mote.agent.worker.v1",
            config_digest=DIGEST,
            incarnation_generation=3,
        ),
        source_session_revision=7,
        record_revision=2,
        materialization_fence=ResidencyFence("residency:agent-1", "owner-1", 9),
        state_snapshot={"context": {"messages": []}},
        mailbox_snapshot={"schema": "mote.agent-mailbox/v1"},
        message_buffer_snapshot=[],
    )


def _raw() -> dict[str, object]:
    raw = json.loads(encode_residency_record(_record()))
    assert isinstance(raw, dict)
    return raw


def test_residency_v1_round_trip_preserves_all_authority_fields() -> None:
    record = _record()
    assert decode_residency_record(encode_residency_record(record), expected_agent_id="agent-1") == record


@pytest.mark.parametrize("field", ["identity", "record_revision", "state_snapshot"])
def test_residency_codec_rejects_missing_and_extra_fields(field: str) -> None:
    missing = _raw()
    del missing[field]
    with pytest.raises(ValueError, match="fields are not canonical"):
        decode_residency_record(json.dumps(missing).encode(), expected_agent_id="agent-1")
    extra = _raw()
    extra["unknown"] = 1
    with pytest.raises(ValueError, match="fields are not canonical"):
        decode_residency_record(json.dumps(extra).encode(), expected_agent_id="agent-1")


def test_residency_codec_rejects_unknown_version_and_agent_mismatch() -> None:
    raw = _raw()
    raw["schema"] = "mote.agent-residency/v2"
    with pytest.raises(ValueError, match="unsupported"):
        decode_residency_record(json.dumps(raw).encode(), expected_agent_id="agent-1")
    with pytest.raises(ValueError, match="identity mismatch"):
        decode_residency_record(encode_residency_record(_record()), expected_agent_id="agent-2")


@pytest.mark.parametrize(
    ("container", "field", "value"),
    [
        ("envelope", "record_revision", True),
        ("envelope", "source_session_revision", "7"),
        ("identity", "incarnation_generation", True),
        ("identity", "config_digest", "bad"),
        ("fence", "fencing_token", 0),
    ],
)
def test_residency_codec_rejects_wrong_primitives(container: str, field: str, value: object) -> None:
    raw = _raw()
    target: dict[str, object]
    if container == "identity":
        candidate = raw["identity"]
        assert isinstance(candidate, dict)
        target = candidate
    elif container == "fence":
        candidate = raw["materialization_fence"]
        assert isinstance(candidate, dict)
        target = candidate
    else:
        target = raw
    target[field] = value
    with pytest.raises((TypeError, ValueError)):
        decode_residency_record(json.dumps(raw).encode(), expected_agent_id="agent-1")


def test_residency_codec_rejects_identity_extra_and_non_json_state() -> None:
    raw = _raw()
    identity = raw["identity"]
    assert isinstance(identity, dict)
    identity["backend_class"] = "attacker.Role"
    with pytest.raises(ValueError, match="fields are not canonical"):
        decode_residency_record(json.dumps(raw).encode(), expected_agent_id="agent-1")

    record = _record()
    with pytest.raises(TypeError, match="JSON-safe"):
        ResidencyRecord(
            identity=record.identity,
            source_session_revision=record.source_session_revision,
            record_revision=record.record_revision,
            materialization_fence=record.materialization_fence,
            state_snapshot={"bad": object()},
            mailbox_snapshot=record.mailbox_snapshot,
            message_buffer_snapshot=record.message_buffer_snapshot,
        )


@pytest.mark.parametrize("payload", [b"{torn", b"\xff"])
def test_residency_codec_rejects_corrupt_bytes(payload: bytes) -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        decode_residency_record(payload, expected_agent_id="agent-1")
