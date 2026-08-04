"""Offline atomic cutover from Agent ingress v1 facts to v2 owners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from mote.runtime.persistence import disk_io

AGENT_INGRESS_MANIFEST_SCHEMA = "mote.agent-ingress-cutover/v2"
_DELIVERY_V1 = "mote.agent-delivery-store/v1"
_TURN_V1 = "mote.agent-turn-queue/v1"
_RESIDENCY_V1 = "mote.agent-residency/v1"


@dataclass(frozen=True, slots=True)
class AgentIngressMigrationReceipt:
    delivery_count: int
    turn_count: int
    residency_count: int
    source_digest: str


def activate_empty_agent_ingress(root: Path) -> None:
    """Publish the initial v2 generation only for a provably empty Product root."""
    manifest_path = root / "agent-ingress-manifest.json"
    if manifest_path.exists():
        raw = _read_json(manifest_path)
        if raw.get("schema") != AGENT_INGRESS_MANIFEST_SCHEMA:
            raise ValueError("Agent ingress activation manifest is unsupported")
        return
    authority_files = tuple(
        path for path in root.glob("*.json") if path.name not in {"agent-lineage.json", "agent-ingress-manifest.json"}
    )
    if authority_files:
        raise ValueError("Agent ingress facts require explicit migration before Product activation")
    disk_io.atomic_write(
        manifest_path,
        _json_bytes(
            {
                "schema": AGENT_INGRESS_MANIFEST_SCHEMA,
                "source_digest": hashlib.sha256(b"empty-agent-ingress-v2").hexdigest(),
                "delivery_count": 0,
                "turn_count": 0,
                "residency_count": 0,
                "evidence_retention_days": 180,
            }
        ),
        fsync=True,
    )


def migrate_agent_ingress_v1(root: Path) -> AgentIngressMigrationReceipt:
    delivery_path = root / "agent-deliveries.json"
    turn_path = root / "agent-turn-queue.json"
    residency_paths = tuple(
        path
        for path in sorted(root.glob("*.json"))
        if path.name not in {delivery_path.name, turn_path.name, "agent-lineage.json", "agent-ingress-manifest.json"}
    )
    delivery_raw = _read_json(delivery_path)
    turn_raw = _read_json(turn_path)
    if set(delivery_raw) != {"schema", "records"} or delivery_raw["schema"] != _DELIVERY_V1:
        raise ValueError("Agent delivery migration requires strict v1 input")
    if set(turn_raw) != {"schema", "queue_id", "revision", "next_enqueue_sequence", "capacity", "scheduling", "items"}:
        raise ValueError("Agent turn migration envelope is not canonical")
    if turn_raw["schema"] != _TURN_V1 or type(turn_raw["items"]) is not list:
        raise ValueError("Agent turn migration requires strict v1 input")
    delivery_records = _delivery_inventory(delivery_raw["records"])
    turns = _turn_inventory(turn_raw["items"])
    bindings: dict[str, str] = {}
    payload_digest_by_turn: dict[str, str] = {}
    for turn in turns:
        identity = cast(dict[str, object], turn["identity"])
        request_id = cast(str, identity["request_id"])
        delivery_ids = cast(list[str], identity["delivery_ids"])
        records = []
        for delivery_id in delivery_ids:
            record = delivery_records.get(delivery_id)
            if record is None:
                raise ValueError("Agent turn references a missing delivery")
            prior = bindings.setdefault(delivery_id, request_id)
            if prior != request_id:
                raise ValueError("Agent delivery is referenced by multiple turns")
            records.append(record)
        payload_digest_by_turn[request_id] = hashlib.sha256(
            "\0".join(
                hashlib.sha256(cast(str, record["message_payload"]).encode()).hexdigest() for record in records
            ).encode()
        ).hexdigest()
    residency_candidates: dict[Path, bytes] = {}
    projected_delivery_ids: set[str] = set()
    for path in residency_paths:
        raw = _read_json(path)
        if raw.get("schema") != _RESIDENCY_V1 or "mailbox_snapshot" not in raw:
            continue
        mailbox = raw["mailbox_snapshot"]
        if (
            type(mailbox) is not dict
            or mailbox.get("schema") != "mote.agent-mailbox/v1"
            or type(mailbox.get("items")) is not list
        ):
            raise ValueError("Agent Residency mailbox inventory is not canonical v1")
        for item in mailbox["items"]:
            if type(item) is not dict or type(item.get("delivery_id")) is not str:
                raise ValueError("Agent Residency mailbox item is invalid")
            delivery_id = item["delivery_id"]
            if delivery_id not in delivery_records:
                raise ValueError("Agent Residency mailbox references a missing delivery")
            projected_delivery_ids.add(delivery_id)
        candidate = dict(raw)
        candidate["schema"] = "mote.agent-residency/v2"
        del candidate["mailbox_snapshot"]
        residency_candidates[path] = _json_bytes(candidate)
    delivery_v2 = []
    for delivery_id, record in sorted(delivery_records.items()):
        candidate = dict(record)
        old_state = candidate.pop("state")
        turn_request_id = bindings.get(delivery_id)
        message_payload = candidate["message_payload"]
        revision = candidate["revision"]
        if type(message_payload) is not str or type(revision) is not int:
            raise ValueError("Agent delivery v1 payload or revision is invalid")
        candidate["payload_digest"] = hashlib.sha256(message_payload.encode()).hexdigest()
        candidate["turn_request_id"] = turn_request_id
        if old_state == "claimed" and turn_request_id is not None:
            candidate["state"] = "bound_to_turn"
        elif old_state in {"acked", "dead_letter"}:
            candidate["state"] = old_state
        elif old_state == "accepted":
            candidate["state"] = "accepted"
            candidate["turn_request_id"] = None
        else:
            raise ValueError("Agent delivery v1 state is unsupported")
        candidate["revision"] = revision + 1
        delivery_v2.append(candidate)
    turn_v2 = []
    for turn in turns:
        candidate = dict(turn)
        identity = cast(dict[str, object], candidate["identity"])
        candidate["payload_digest"] = payload_digest_by_turn[cast(str, identity["request_id"])]
        candidate["settlement_state"] = None
        turn_v2.append(candidate)
    for candidate in delivery_v2:
        candidate["accepted_at"] = datetime.fromtimestamp(0, tz=timezone.utc).isoformat()
        candidate["terminal_at"] = None
    delivery_candidate = _json_bytes({"schema": "mote.agent-delivery-store/v3", "records": delivery_v2})
    turn_candidate = _json_bytes({**turn_raw, "schema": "mote.agent-turn-queue/v2", "items": turn_v2})
    sources = {delivery_path: delivery_path.read_bytes(), turn_path: turn_path.read_bytes()}
    sources.update({path: path.read_bytes() for path in residency_candidates})
    source_digest = hashlib.sha256(
        b"".join(path.name.encode() + b"\0" + payload for path, payload in sorted(sources.items()))
    ).hexdigest()
    for path, payload in sources.items():
        evidence = root / f"{path.name}.v1-evidence-{hashlib.sha256(payload).hexdigest()}"
        if evidence.exists() and evidence.read_bytes() != payload:
            raise ValueError("Agent ingress migration evidence conflicts with source")
        if not evidence.exists():
            disk_io.atomic_write(evidence, payload, fsync=True)
    candidates = {delivery_path: delivery_candidate, turn_path: turn_candidate, **residency_candidates}
    for path, payload in candidates.items():
        disk_io.atomic_write(path.with_name(f".{path.name}.v2-candidate"), payload, fsync=True)
        if path.with_name(f".{path.name}.v2-candidate").read_bytes() != payload:
            raise RuntimeError("Agent ingress candidate read-back failed")
    for path in sorted(candidates):
        path.with_name(f".{path.name}.v2-candidate").replace(path)
    manifest = {
        "schema": AGENT_INGRESS_MANIFEST_SCHEMA,
        "source_digest": source_digest,
        "delivery_count": len(delivery_v2),
        "turn_count": len(turn_v2),
        "residency_count": len(residency_candidates),
        "evidence_retention_days": 180,
    }
    disk_io.atomic_write(root / "agent-ingress-manifest.json", _json_bytes(manifest), fsync=True)
    return AgentIngressMigrationReceipt(len(delivery_v2), len(turn_v2), len(residency_candidates), source_digest)


def _delivery_inventory(raw: object) -> dict[str, dict[str, object]]:
    fields = {
        "delivery_id",
        "target_agent_id",
        "target_generation",
        "mode",
        "message_payload",
        "state",
        "revision",
        "fencing_token",
        "reason",
    }
    if type(raw) is not list:
        raise ValueError("Agent delivery records must be an array")
    result = {}
    for item in raw:
        if (
            type(item) is not dict
            or set(item) != fields
            or type(item["delivery_id"]) is not str
            or type(item["message_payload"]) is not str
            or type(item["revision"]) is not int
        ):
            raise ValueError("Agent delivery v1 record is not canonical")
        if item["delivery_id"] in result:
            raise ValueError("Agent delivery v1 identity is duplicated")
        result[item["delivery_id"]] = item
    return result


def _turn_inventory(raw: object) -> tuple[dict[str, object], ...]:
    if type(raw) is not list:
        raise ValueError("Agent turn records must be an array")
    result = []
    for item in raw:
        if type(item) is not dict or type(item.get("identity")) is not dict:
            raise ValueError("Agent turn v1 record is invalid")
        identity = item["identity"]
        if set(identity) != {"queue_id", "request_id", "root_id", "subtree_id", "agent_id", "delivery_ids"}:
            raise ValueError("Agent turn v1 identity is invalid")
        if type(identity["request_id"]) is not str or type(identity["delivery_ids"]) is not list:
            raise ValueError("Agent turn v1 identity primitives are invalid")
        result.append(item)
    return tuple(result)


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Agent ingress source {path.name} is unreadable") from exc
    if type(raw) is not dict:
        raise ValueError(f"Agent ingress source {path.name} is not an object")
    return raw


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


__all__ = [
    "AGENT_INGRESS_MANIFEST_SCHEMA",
    "AgentIngressMigrationReceipt",
    "activate_empty_agent_ingress",
    "migrate_agent_ingress_v1",
]
