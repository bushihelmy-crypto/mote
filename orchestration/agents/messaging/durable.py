"""Canonical fenced durable store for accepted Agent delivery."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from mote.contracts.agent.delivery import AgentDeliveryRecord, AgentDeliveryState
from mote.contracts.conversation import Message
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch

_SCHEMA = "mote.agent-delivery-store/v1"


class AgentDeliveryStore:
    def __init__(self, path: Path, *, leases: LeaseCoordinator, subject: str) -> None:
        self._path = path
        self._leases = leases
        self._subject = subject

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
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            current = records.get(delivery_id)
            if current is not None:
                return current
            record = AgentDeliveryRecord(
                delivery_id,
                target_agent_id,
                0,
                mode,
                message.dump(),
                AgentDeliveryState.ACCEPTED,
                1,
                lease.fencing_token,
            )
            records[delivery_id] = record
            self._write(records)
            return record

    def claim(self, delivery_id: str, target_generation: int, *, lease: LeaseEpoch) -> AgentDeliveryRecord:
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            current = records.get(delivery_id)
            if current is None or current.state not in {AgentDeliveryState.ACCEPTED, AgentDeliveryState.CLAIMED}:
                raise RuntimeError("delivery claim conflicts with canonical state")
            if current.state is AgentDeliveryState.CLAIMED and current.target_generation == target_generation:
                return current
            updated = AgentDeliveryRecord(
                current.delivery_id,
                current.target_agent_id,
                target_generation,
                current.mode,
                current.message_payload,
                AgentDeliveryState.CLAIMED,
                current.revision + 1,
                lease.fencing_token,
                "",
            )
            records[delivery_id] = updated
            self._write(records)
            return updated

    def ack(self, delivery_id: str, target_generation: int, *, lease: LeaseEpoch) -> AgentDeliveryRecord:
        return self._transition(
            delivery_id,
            {AgentDeliveryState.CLAIMED, AgentDeliveryState.ACKED},
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
                records[delivery_id] = AgentDeliveryRecord(
                    record.delivery_id,
                    record.target_agent_id,
                    record.target_generation,
                    record.mode,
                    record.message_payload,
                    AgentDeliveryState.DEAD_LETTER,
                    record.revision + 1,
                    lease.fencing_token,
                    reason,
                )
                changed = True
            if changed:
                self._write(records)

    def pending(self) -> tuple[AgentDeliveryRecord, ...]:
        return tuple(
            record
            for record in self._read().values()
            if record.state in {AgentDeliveryState.ACCEPTED, AgentDeliveryState.CLAIMED}
        )

    def records(self) -> tuple[AgentDeliveryRecord, ...]:
        return tuple(self._read().values())

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
            updated = AgentDeliveryRecord(
                current.delivery_id,
                current.target_agent_id,
                current.target_generation,
                current.mode,
                current.message_payload,
                state,
                current.revision + 1,
                lease.fencing_token,
                reason,
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
            or raw["schema"] != _SCHEMA
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
            "state",
            "revision",
            "fencing_token",
            "reason",
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
            if (
                type(item["message_payload"]) is not str
                or not item["message_payload"]
                or type(item["reason"]) is not str
            ):
                raise ValueError("delivery payload is invalid")
            record = AgentDeliveryRecord(
                item["delivery_id"],
                item["target_agent_id"],
                item["target_generation"],
                item["mode"],
                item["message_payload"],
                AgentDeliveryState(item["state"]),
                item["revision"],
                item["fencing_token"],
                item["reason"],
            )
            if record.delivery_id in records:
                raise ValueError("duplicate delivery identity")
            try:
                message = Message.load(record.message_payload)
            except (TypeError, ValueError) as exc:
                raise ValueError("delivery message payload is invalid") from exc
            if message is None:
                raise ValueError("delivery message payload is invalid")
            records[record.delivery_id] = record
        return records

    def _write(self, records: dict[str, AgentDeliveryRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "records": [
                {
                    "delivery_id": record.delivery_id,
                    "target_agent_id": record.target_agent_id,
                    "target_generation": record.target_generation,
                    "mode": record.mode,
                    "message_payload": record.message_payload,
                    "state": record.state.value,
                    "revision": record.revision,
                    "fencing_token": record.fencing_token,
                    "reason": record.reason,
                }
                for record in sorted(records.values(), key=lambda value: value.delivery_id)
            ],
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


__all__ = ["AgentDeliveryStore"]
