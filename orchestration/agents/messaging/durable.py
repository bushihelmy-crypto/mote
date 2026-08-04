"""Canonical fenced durable store for accepted Agent delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mote.contracts.agent.delivery import AgentDeliveryRecord, AgentDeliveryState
from mote.contracts.conversation import Message, dump_message, load_message
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.runtime.persistence import disk_io

DELIVERY_SCHEMA = "mote.agent-delivery-store/v3"
_MAX_RECORDS = 4096
_MAX_PAYLOAD_BYTES = 1_048_576


class AgentDeliveryStore:
    def __init__(self, path: Path, *, leases: LeaseCoordinator, subject: str, clock=None) -> None:
        self._path = path
        self._leases = leases
        self._subject = subject
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def identity(target_agent_id: str, message: Message) -> str:
        material = f"{message.sent_from}\0{target_agent_id}\0{message.id}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def accept(
        self,
        target_agent_id: str,
        message: Message,
        mode: str,
        *,
        lease: LeaseEpoch,
    ) -> AgentDeliveryRecord:
        delivery_id = self.identity(target_agent_id, message)
        message_payload = dump_message(message)
        if len(message_payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("delivery payload exceeds the configured bound")
        payload_digest = hashlib.sha256(message_payload.encode("utf-8")).hexdigest()
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            current = records.get(delivery_id)
            if current is not None:
                if current.payload_digest != payload_digest or current.mode != mode:
                    raise ValueError("delivery identity conflicts with its canonical preimage")
                return current
            if len(records) >= _MAX_RECORDS:
                raise RuntimeError("delivery store is backpressured")
            record = AgentDeliveryRecord(
                delivery_id,
                target_agent_id,
                0,
                mode,
                message_payload,
                payload_digest,
                AgentDeliveryState.ACCEPTED,
                1,
                lease.fencing_token,
                accepted_at=self._clock(),
            )
            records[delivery_id] = record
            self._write(records)
            return record

    def bind_to_turn(
        self,
        delivery_ids: tuple[str, ...],
        *,
        turn_request_id: str,
        target_generation: int,
        expected_payload_digest: str,
        lease: LeaseEpoch,
    ) -> tuple[AgentDeliveryRecord, ...]:
        if not delivery_ids or len(delivery_ids) != len(set(delivery_ids)):
            raise ValueError("delivery bind batch is invalid")
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            batch = tuple(records.get(delivery_id) for delivery_id in delivery_ids)
            if any(record is None for record in batch):
                raise RuntimeError("delivery bind references an unknown delivery")
            concrete = tuple(record for record in batch if record is not None)
            digest = hashlib.sha256("\0".join(record.payload_digest for record in concrete).encode()).hexdigest()
            if digest != expected_payload_digest:
                raise RuntimeError("delivery bind payload digest conflicts with turn preimage")
            for current in concrete:
                if current.state is AgentDeliveryState.BOUND_TO_TURN:
                    if current.turn_request_id != turn_request_id or current.target_generation != target_generation:
                        raise RuntimeError("delivery is bound to another turn")
                    continue
                if current.state is not AgentDeliveryState.ACCEPTED:
                    raise RuntimeError("delivery bind conflicts with canonical state")
                records[current.delivery_id] = replace(
                    current,
                    target_generation=target_generation,
                    state=AgentDeliveryState.BOUND_TO_TURN,
                    revision=current.revision + 1,
                    fencing_token=lease.fencing_token,
                    turn_request_id=turn_request_id,
                )
            self._write(records)
            return tuple(records[delivery_id] for delivery_id in delivery_ids)

    def ack(self, delivery_id: str, target_generation: int, *, lease: LeaseEpoch) -> AgentDeliveryRecord:
        return self._transition(
            delivery_id,
            {AgentDeliveryState.BOUND_TO_TURN, AgentDeliveryState.ACKED},
            AgentDeliveryState.ACKED,
            target_generation,
            "",
            lease,
        )

    def dead_letter_target(self, target_agent_id: str, reason: str, *, lease: LeaseEpoch) -> None:
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            changed = False
            for delivery_id, record in tuple(records.items()):
                if record.target_agent_id != target_agent_id or record.state in {
                    AgentDeliveryState.ACKED,
                    AgentDeliveryState.DEAD_LETTER,
                }:
                    continue
                records[delivery_id] = replace(
                    record,
                    state=AgentDeliveryState.DEAD_LETTER,
                    revision=record.revision + 1,
                    fencing_token=lease.fencing_token,
                    reason=reason,
                    terminal_at=record.terminal_at or self._clock(),
                )
                changed = True
            if changed:
                self._write(records)

    def pending(self) -> tuple[AgentDeliveryRecord, ...]:
        return tuple(
            record
            for record in self._read().values()
            if record.state in {AgentDeliveryState.ACCEPTED, AgentDeliveryState.BOUND_TO_TURN}
        )

    def records(self) -> tuple[AgentDeliveryRecord, ...]:
        return tuple(self._read().values())

    def retain(self, *, lease: LeaseEpoch, now: datetime | None = None, limit: int = 256) -> int:
        instant = now or self._clock()
        changed = 0
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            for key, record in sorted(records.items()):
                if changed >= limit or record.terminal_at is None:
                    continue
                age = instant - record.terminal_at
                if age >= timedelta(days=180):
                    records[key] = replace(
                        record,
                        state=AgentDeliveryState.TOMBSTONED,
                        message_payload="",
                        revision=record.revision + 1,
                        fencing_token=lease.fencing_token,
                    )
                    changed += 1
                elif age >= timedelta(days=30) and record.message_payload:
                    records[key] = replace(
                        record, message_payload="", revision=record.revision + 1, fencing_token=lease.fencing_token
                    )
                    changed += 1
            if changed:
                self._write(records)
        return changed

    def _transition(self, delivery_id, allowed, state, target_generation, reason, lease):
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            current = records.get(delivery_id)
            if current is None or current.state not in allowed:
                raise RuntimeError("delivery transition conflicts with canonical state")
            if current.target_generation != target_generation:
                raise RuntimeError("delivery target generation is stale")
            if current.state is state:
                return current
            updated = replace(
                current,
                state=state,
                revision=current.revision + 1,
                fencing_token=lease.fencing_token,
                reason=reason,
                terminal_at=current.terminal_at or self._clock(),
            )
            records[delivery_id] = updated
            self._write(records)
            return updated

    def _read(self) -> dict[str, AgentDeliveryRecord]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "records"}
            or raw["schema"] != DELIVERY_SCHEMA
            or type(raw["records"]) is not list
        ):
            raise ValueError("delivery store envelope is invalid")
        records: dict[str, AgentDeliveryRecord] = {}
        fields = {
            "delivery_id",
            "target_agent_id",
            "target_generation",
            "mode",
            "message_payload",
            "payload_digest",
            "state",
            "revision",
            "fencing_token",
            "turn_request_id",
            "reason",
            "accepted_at",
            "terminal_at",
        }
        for item in raw["records"]:
            if type(item) is not dict or set(item) != fields:
                raise ValueError("delivery record shape is invalid")
            if any(type(item[key]) is not str or not item[key] for key in ("delivery_id", "target_agent_id", "mode")):
                raise ValueError("delivery identity is invalid")
            if (
                type(item["target_generation"]) is not int
                or item["target_generation"] < 0
                or type(item["revision"]) is not int
                or item["revision"] < 1
                or type(item["fencing_token"]) is not int
            ):
                raise ValueError("delivery generation/revision is invalid")
            if type(item["message_payload"]) is not str or type(item["reason"]) is not str:
                raise ValueError("delivery payload is invalid")
            record = AgentDeliveryRecord(
                item["delivery_id"],
                item["target_agent_id"],
                item["target_generation"],
                item["mode"],
                item["message_payload"],
                item["payload_digest"],
                AgentDeliveryState(item["state"]),
                item["revision"],
                item["fencing_token"],
                item["turn_request_id"],
                item["reason"],
                datetime.fromisoformat(item["accepted_at"]),
                None if item["terminal_at"] is None else datetime.fromisoformat(item["terminal_at"]),
            )
            if record.delivery_id in records:
                raise ValueError("duplicate delivery identity")
            if record.message_payload:
                try:
                    message = load_message(record.message_payload)
                except (TypeError, ValueError) as exc:
                    raise ValueError("delivery message payload is invalid") from exc
                if message is None:
                    raise ValueError("delivery message payload is invalid")
            records[record.delivery_id] = record
        return records

    def _write(self, records: dict[str, AgentDeliveryRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": DELIVERY_SCHEMA,
            "records": [
                {
                    "delivery_id": record.delivery_id,
                    "target_agent_id": record.target_agent_id,
                    "target_generation": record.target_generation,
                    "mode": record.mode,
                    "message_payload": record.message_payload,
                    "payload_digest": record.payload_digest,
                    "state": record.state.value,
                    "revision": record.revision,
                    "fencing_token": record.fencing_token,
                    "turn_request_id": record.turn_request_id,
                    "reason": record.reason,
                    "accepted_at": record.accepted_at.isoformat() if record.accepted_at else None,
                    "terminal_at": record.terminal_at.isoformat() if record.terminal_at else None,
                }
                for record in sorted(records.values(), key=lambda value: value.delivery_id)
            ],
        }
        disk_io.atomic_write(
            self._path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            fsync=True,
        )


__all__ = ["AgentDeliveryStore", "DELIVERY_SCHEMA"]
