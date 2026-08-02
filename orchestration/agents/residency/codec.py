"""Strict v1 wire codec for durable Residency records."""

from __future__ import annotations

import json
from typing import Mapping, TypeAlias, cast

from mote.contracts.content import ContentDigest
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.orchestration.agents.residency.model import (
    ResidencyFence,
    ResidencyIdentity,
    ResidencyLifecycle,
    ResidencyRecord,
)

RESIDENCY_SCHEMA = "mote.agent-residency/v1"
JsonObject: TypeAlias = dict[str, object]

_ENVELOPE_FIELDS = {
    "schema",
    "identity",
    "source_session_revision",
    "record_revision",
    "materialization_fence",
    "state_snapshot",
    "mailbox_snapshot",
    "message_buffer_snapshot",
    "lifecycle",
    "install_fence",
}
_IDENTITY_FIELDS = {
    "logical_agent_id",
    "root_agent_id",
    "parent_agent_id",
    "agent_path",
    "nickname",
    "definition_id",
    "config_digest",
    "incarnation_generation",
}
_FENCE_FIELDS = {"subject", "owner_id", "fencing_token"}


def encode_residency_record(record: ResidencyRecord) -> bytes:
    identity = record.identity
    fence = record.materialization_fence
    payload = {
        "schema": RESIDENCY_SCHEMA,
        "identity": {
            "logical_agent_id": identity.logical_agent_id,
            "root_agent_id": identity.root_agent_id,
            "parent_agent_id": identity.parent_agent_id,
            "agent_path": identity.agent_path,
            "nickname": identity.nickname,
            "definition_id": identity.definition_id,
            "config_digest": str(identity.config_digest),
            "incarnation_generation": identity.incarnation_generation,
        },
        "source_session_revision": record.source_session_revision,
        "record_revision": record.record_revision,
        "materialization_fence": {
            "subject": fence.subject,
            "owner_id": fence.owner_id,
            "fencing_token": fence.fencing_token,
        },
        "state_snapshot": thaw_json(cast(JsonValue, record.state_snapshot)),
        "mailbox_snapshot": thaw_json(cast(JsonValue, record.mailbox_snapshot)),
        "message_buffer_snapshot": thaw_json(record.message_buffer_snapshot),
        "lifecycle": record.lifecycle.value,
        "install_fence": (
            None
            if record.install_fence is None
            else {
                "subject": record.install_fence.subject,
                "owner_id": record.install_fence.owner_id,
                "fencing_token": record.install_fence.fencing_token,
            }
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_residency_record(data: bytes, *, expected_agent_id: str) -> ResidencyRecord:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Residency record is not canonical JSON") from exc
    envelope = _mapping(raw, _ENVELOPE_FIELDS, "Residency envelope")
    if envelope["schema"] != RESIDENCY_SCHEMA:
        raise ValueError("Residency schema is unsupported")
    identity_raw = _mapping(envelope["identity"], _IDENTITY_FIELDS, "Residency identity")
    logical_agent_id = _string(identity_raw["logical_agent_id"], "logical_agent_id")
    if logical_agent_id != expected_agent_id:
        raise ValueError("Residency logical Agent identity mismatch")
    identity = ResidencyIdentity(
        logical_agent_id=logical_agent_id,
        root_agent_id=_string(identity_raw["root_agent_id"], "root_agent_id"),
        parent_agent_id=_optional_string(identity_raw["parent_agent_id"], "parent_agent_id"),
        agent_path=_string(identity_raw["agent_path"], "agent_path"),
        nickname=_optional_string(identity_raw["nickname"], "nickname"),
        definition_id=_string(identity_raw["definition_id"], "definition_id"),
        config_digest=ContentDigest(_string(identity_raw["config_digest"], "config_digest")),
        incarnation_generation=_integer(identity_raw["incarnation_generation"], "incarnation_generation"),
    )
    fence_raw = _mapping(envelope["materialization_fence"], _FENCE_FIELDS, "Residency materialization fence")
    state = _json_object(envelope["state_snapshot"], "state_snapshot")
    mailbox = _json_object(envelope["mailbox_snapshot"], "mailbox_snapshot")
    messages = freeze_json(envelope["message_buffer_snapshot"], path="message_buffer_snapshot")
    lifecycle_raw = _string(envelope["lifecycle"], "lifecycle")
    try:
        lifecycle = ResidencyLifecycle(lifecycle_raw)
    except ValueError as exc:
        raise ValueError("Residency lifecycle is unsupported") from exc
    install_fence_raw = envelope["install_fence"]
    install_fence = None
    if install_fence_raw is not None:
        install = _mapping(install_fence_raw, _FENCE_FIELDS, "Residency install fence")
        install_fence = ResidencyFence(
            _string(install["subject"], "install fence subject"),
            _string(install["owner_id"], "install fence owner_id"),
            _integer(install["fencing_token"], "install fencing_token"),
        )
    return ResidencyRecord(
        identity=identity,
        source_session_revision=_integer(envelope["source_session_revision"], "source_session_revision"),
        record_revision=_integer(envelope["record_revision"], "record_revision"),
        materialization_fence=ResidencyFence(
            subject=_string(fence_raw["subject"], "fence subject"),
            owner_id=_string(fence_raw["owner_id"], "fence owner_id"),
            fencing_token=_integer(fence_raw["fencing_token"], "fencing_token"),
        ),
        state_snapshot=state,
        mailbox_snapshot=mailbox,
        message_buffer_snapshot=messages,
        lifecycle=lifecycle,
        install_fence=install_fence,
    )


def _mapping(raw: object, fields: set[str], label: str) -> JsonObject:
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError(f"{label} fields are not canonical")
    return cast(JsonObject, raw)


def _json_object(raw: object, label: str) -> Mapping[str, JsonValue]:
    frozen = freeze_json(raw, path=label)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"Residency {label} must be a JSON object")
    return cast(Mapping[str, JsonValue], frozen)


def _string(raw: object, label: str) -> str:
    if type(raw) is not str or not raw:
        raise ValueError(f"Residency {label} primitive is invalid")
    return raw


def _optional_string(raw: object, label: str) -> str | None:
    return None if raw is None else _string(raw, label)


def _integer(raw: object, label: str) -> int:
    if type(raw) is not int or raw < 1:
        raise ValueError(f"Residency {label} primitive is invalid")
    return raw


__all__ = ["RESIDENCY_SCHEMA", "decode_residency_record", "encode_residency_record"]
