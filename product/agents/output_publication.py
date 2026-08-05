"""Product-owned durable outbox for final Agent output publication."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from mote.contracts.conversation import dump_message, load_message
from mote.contracts.output.publication import (
    OutputPublicationDisposition,
    OutputPublicationReceipt,
    OutputPublicationRequest,
)
from mote.contracts.ports.agent.routing import AgentRoutingPort
from mote.contracts.ports.output.publication import OutputPublisher
from mote.contracts.runtime.errors import LeaseUnavailableError
from mote.runtime.control.leases import FileLeaseCoordinator
from mote.runtime.persistence import disk_io
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.workspace.store import SessionWorkspace

_SCHEMA = "mote.output-publication-outbox/v1"
_MAX_ATTEMPTS = 5
_MAX_RECORDS = 4096
_LEASE_TTL_SECONDS = 30.0


class _State(StrEnum):
    PENDING = "pending"
    ACKED = "acked"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class _Record:
    request: OutputPublicationRequest
    payload_digest: str
    state: _State
    attempts: int
    last_error: str
    accepted_at: datetime
    settled_at: datetime | None


class DurableOutputPublisher(OutputPublisher):
    """Persist publication intent before invoking the concrete routing adapter."""

    def __init__(self, path: Path, routing: AgentRoutingPort | None) -> None:
        self._path = path
        self._routing = routing
        self._owner_id = f"output-publisher:{uuid.uuid4().hex}"
        self._subject = f"output-publication:{path.parent.name}"
        self._leases = FileLeaseCoordinator(path.with_name("output-publication-leases.json"))

    async def accept(self, request: OutputPublicationRequest) -> OutputPublicationReceipt:
        try:
            return await run_disk_io(self._accept, request)
        except LeaseUnavailableError:
            return await run_disk_io(self._existing_receipt, request)

    async def reconcile_once(self) -> bool:
        if self._routing is None:
            return False
        try:
            pending = await run_disk_io(self._pending_owned)
        except LeaseUnavailableError:
            return False
        complete = True
        try:
            for record in pending:
                try:
                    self._routing.publish_message(record.request.message)
                except Exception as exc:
                    complete = False
                    await run_disk_io(self._record_failure, record.request.publication_id, str(exc))
                else:
                    await run_disk_io(self._acknowledge, record.request.publication_id)
            return complete
        finally:
            await run_disk_io(self._release_owner)

    def _accept(self, request: OutputPublicationRequest) -> OutputPublicationReceipt:
        payload = self._request_payload(request)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        lease = self._leases.acquire(self._subject, self._owner_id, _LEASE_TTL_SECONDS)
        try:
            with self._leases.guard(self._subject, lease.fencing_token):
                records = self._read()
                current = records.get(request.publication_id)
                if current is not None:
                    if current.payload_digest != digest:
                        raise ValueError("output publication identity conflicts with its canonical request")
                    disposition = (
                        OutputPublicationDisposition.ALREADY_SETTLED
                        if current.state in {_State.ACKED, _State.DEAD_LETTER}
                        else OutputPublicationDisposition.ALREADY_ACCEPTED
                    )
                    return OutputPublicationReceipt(request.publication_id, disposition)
                if len(records) >= _MAX_RECORDS:
                    raise RuntimeError("output publication outbox is backpressured")
                records[request.publication_id] = _Record(
                    request,
                    digest,
                    _State.PENDING,
                    0,
                    "",
                    datetime.now(timezone.utc),
                    None,
                )
                self._write(records)
        finally:
            self._leases.release(lease)
        return OutputPublicationReceipt(request.publication_id, OutputPublicationDisposition.ACCEPTED)

    def _existing_receipt(self, request: OutputPublicationRequest) -> OutputPublicationReceipt:
        records = self._read()
        current = records.get(request.publication_id)
        if current is None or self._request_payload(current.request) != self._request_payload(request):
            raise LeaseUnavailableError("output publication owner is busy and no matching intent exists")
        disposition = (
            OutputPublicationDisposition.ALREADY_SETTLED
            if current.state in {_State.ACKED, _State.DEAD_LETTER}
            else OutputPublicationDisposition.ALREADY_ACCEPTED
        )
        return OutputPublicationReceipt(request.publication_id, disposition)

    def _pending_owned(self) -> tuple[_Record, ...]:
        lease = self._leases.acquire(self._subject, self._owner_id, _LEASE_TTL_SECONDS)
        try:
            with self._leases.guard(self._subject, lease.fencing_token):
                return tuple(record for record in self._read().values() if record.state is _State.PENDING)
        except BaseException:
            self._leases.release(lease)
            raise

    def _release_owner(self) -> None:
        lease = self._leases.get(self._subject)
        if lease is not None and lease.owner_id == self._owner_id:
            self._leases.release(lease)

    def _acknowledge(self, publication_id: str) -> None:
        self._settle(publication_id, error="")

    def _record_failure(self, publication_id: str, error: str) -> None:
        self._settle(publication_id, error=error or "routing adapter failed")

    def _settle(self, publication_id: str, *, error: str) -> None:
        lease = self._leases.acquire(self._subject, self._owner_id, _LEASE_TTL_SECONDS)
        with self._leases.guard(self._subject, lease.fencing_token):
            records = self._read()
            current = records.get(publication_id)
            if current is None:
                raise RuntimeError("output publication does not exist")
            if current.state is not _State.PENDING:
                return
            attempts = current.attempts + 1
            state = _State.ACKED if not error else _State.DEAD_LETTER if attempts >= _MAX_ATTEMPTS else _State.PENDING
            records[publication_id] = _Record(
                current.request,
                current.payload_digest,
                state,
                attempts,
                error,
                current.accepted_at,
                datetime.now(timezone.utc) if state is not _State.PENDING else None,
            )
            self._write(records)

    def _read(self) -> dict[str, _Record]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if type(raw) is not dict or set(raw) != {"schema", "records"} or raw["schema"] != _SCHEMA:
            raise ValueError("output publication outbox envelope is invalid")
        items = raw["records"]
        if type(items) is not list:
            raise ValueError("output publication records must be a list")
        records: dict[str, _Record] = {}
        fields = {
            "publication_id",
            "source_agent_id",
            "candidate_id",
            "contract_id",
            "run_id",
            "run_kind",
            "message",
            "payload_digest",
            "state",
            "attempts",
            "last_error",
            "accepted_at",
            "settled_at",
        }
        for item in items:
            if type(item) is not dict or set(item) != fields:
                raise ValueError("output publication record shape is invalid")
            text_fields = fields - {"message", "attempts", "settled_at"}
            if any(type(item[name]) is not str for name in text_fields):
                raise ValueError("output publication record field is invalid")
            if type(item["attempts"]) is not int or item["attempts"] < 0:
                raise ValueError("output publication attempts are invalid")
            message = load_message(item["message"])
            request = OutputPublicationRequest(
                item["publication_id"],
                item["source_agent_id"],
                item["candidate_id"],
                item["contract_id"],
                item["run_id"],
                item["run_kind"],
                message,
            )
            record = _Record(
                request,
                item["payload_digest"],
                _State(item["state"]),
                item["attempts"],
                item["last_error"],
                datetime.fromisoformat(item["accepted_at"]),
                None if item["settled_at"] is None else datetime.fromisoformat(item["settled_at"]),
            )
            if record.accepted_at.tzinfo is None or record.accepted_at.utcoffset() is None:
                raise ValueError("output publication accepted_at must be timezone-aware")
            if record.settled_at is not None and (
                record.settled_at.tzinfo is None or record.settled_at.utcoffset() is None
            ):
                raise ValueError("output publication settled_at must be timezone-aware")
            if (record.state is _State.PENDING) != (record.settled_at is None):
                raise ValueError("output publication terminal instant conflicts with state")
            if record.attempts > _MAX_ATTEMPTS:
                raise ValueError("output publication attempts exceed the canonical bound")
            if request.publication_id in records:
                raise ValueError("duplicate output publication identity")
            if (
                record.payload_digest
                != hashlib.sha256(
                    json.dumps(self._request_payload(request), sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
            ):
                raise ValueError("output publication payload digest is invalid")
            records[request.publication_id] = record
        return records

    def _write(self, records: dict[str, _Record]) -> None:
        payload = {
            "schema": _SCHEMA,
            "records": [
                {
                    **self._request_payload(record.request),
                    "payload_digest": record.payload_digest,
                    "state": record.state.value,
                    "attempts": record.attempts,
                    "last_error": record.last_error,
                    "accepted_at": record.accepted_at.isoformat(),
                    "settled_at": None if record.settled_at is None else record.settled_at.isoformat(),
                }
                for record in sorted(records.values(), key=lambda item: item.request.publication_id)
            ],
        }
        disk_io.atomic_write(
            self._path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            fsync=True,
        )

    @staticmethod
    def _request_payload(request: OutputPublicationRequest) -> dict[str, str]:
        return {
            "publication_id": request.publication_id,
            "source_agent_id": request.source_agent_id,
            "candidate_id": request.candidate_id,
            "contract_id": request.contract_id,
            "run_id": request.run_id,
            "run_kind": request.run_kind,
            "message": dump_message(request.message),
        }


@dataclass(frozen=True, slots=True)
class ProductOutputPublisherFactory:
    workspace_root: Path

    def build(self, session_id: str, routing: AgentRoutingPort | None) -> DurableOutputPublisher:
        path = SessionWorkspace(self.workspace_root).session_dir(session_id) / "output-publications.json"
        return DurableOutputPublisher(path, routing)


__all__ = ["DurableOutputPublisher", "ProductOutputPublisherFactory"]
