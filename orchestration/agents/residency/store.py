"""Fenced strict durable storage for evicted Agent incarnation state."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Mapping, cast

from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.ports.agent.hosting import DurableWritePort
from mote.contracts.ports.agent.residency import ResidentAgentFactory, ResidentAgentStatePort
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import Mailbox
from mote.orchestration.agents.residency.codec import decode_residency_record, encode_residency_record
from mote.orchestration.agents.residency.model import (
    ResidencyFence,
    ResidencyIdentity,
    ResidencyLifecycle,
    ResidencyRecord,
)
from mote.runtime.persistence import DiskWriter, atomic_write
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay


class ResidencyStoreError(RuntimeError):
    """Residency state is corrupt, conflicting, or cannot be committed safely."""


@dataclass(frozen=True, slots=True)
class RehydratedResidency:
    agent: ResidentAgentStatePort
    mailbox: Mailbox
    install_record_revision: int


class ResidencyStore:
    def __init__(
        self,
        base_dir: str,
        *,
        sessions_base_dir: str,
        lease_coordinator: LeaseCoordinator,
        writer: DurableWritePort | None = None,
    ) -> None:
        if not base_dir or not sessions_base_dir:
            raise ValueError("ResidencyStore requires explicit residency and session directories")
        self._base = Path(base_dir)
        self._sessions_base_dir = sessions_base_dir
        self._lease_coordinator = lease_coordinator
        self._writer = writer or DiskWriter()

    def _path(self, session_id: str) -> Path:
        return self._base / f"{session_id}.json"

    def _lock_path(self, session_id: str) -> Path:
        return self._base / f"{session_id}.lock"

    def _session_log(self, session_id: str) -> SessionLog:
        return SessionLog(session_id, base_dir=self._sessions_base_dir, writer=self._writer)

    def has(self, session_id: str) -> bool:
        return self._path(session_id).is_file()

    async def materialize(
        self,
        runtime: AgentRuntime,
        *,
        identity: ResidencyIdentity,
        lease: LeaseEpoch,
    ) -> ResidencyRecord:
        agent = runtime.role
        if not isinstance(agent, ResidentAgentStatePort):
            raise TypeError("Agent does not implement the Residency state Port")
        self._validate_agent_identity(agent, identity)
        session_log = self._session_log(identity.logical_agent_id)
        if not session_log.exists() or session_log.committed_version < 1:
            raise ResidencyStoreError("Residency requires a committed canonical Session stream")
        state = agent.export_residency_state(session_history_is_durable=True)
        try:
            message_buffer = json.loads(await runtime.msg_buffer.dump())
        except json.JSONDecodeError as exc:
            raise ResidencyStoreError("Agent message buffer snapshot is invalid") from exc
        with self._locked(identity.logical_agent_id):
            self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
            previous = self._read_unlocked(identity.logical_agent_id)
            if previous is not None:
                if previous.identity != identity:
                    raise ResidencyStoreError("Residency identity changed across materialization")
                if previous.materialization_fence.fencing_token > lease.fencing_token:
                    raise ResidencyStoreError("Residency materialization fence moved backwards")
                if previous.lifecycle is ResidencyLifecycle.INSTALLING:
                    raise ResidencyStoreError("Residency install claim is not settled")
            record = ResidencyRecord(
                identity=identity,
                source_session_revision=session_log.committed_version,
                record_revision=1 if previous is None else previous.record_revision + 1,
                materialization_fence=ResidencyFence(lease.subject, lease.owner_id, lease.fencing_token),
                state_snapshot=state,
                message_buffer_snapshot=freeze_json(message_buffer, path="residency.message_buffer_snapshot"),
            )
            data = encode_residency_record(record)
            path = self._path(identity.logical_agent_id)
            await self._writer.submit(str(path), lambda: atomic_write(path, data))
            return record

    def read_record(self, session_id: str) -> ResidencyRecord | None:
        with self._locked(session_id):
            return self._read_unlocked(session_id)

    def rehydrate(
        self,
        expected_identity: ResidencyIdentity,
        *,
        factory: ResidentAgentFactory,
        lease: LeaseEpoch,
    ) -> RehydratedResidency | None:
        session_id = expected_identity.logical_agent_id
        with self._locked(session_id):
            self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
            record = self._read_unlocked(session_id)
            if record is None:
                return None
            if record.identity != expected_identity:
                raise ResidencyStoreError("Residency identity does not match trusted composition")
            if factory.definition_id != record.identity.definition_id:
                raise ResidencyStoreError("Residency definition identity mismatch")
            if factory.config_digest != record.identity.config_digest:
                raise ResidencyStoreError("Residency configuration identity mismatch")
            if lease.subject != record.materialization_fence.subject:
                raise ResidencyStoreError("Residency lease subject mismatch")
            if lease.fencing_token < record.materialization_fence.fencing_token:
                raise ResidencyStoreError("Residency rehydrate fence is stale")
            agent = factory.build(record.state_snapshot)
            self._validate_agent_identity(agent, expected_identity)
            replayed = replay(self._session_log(session_id))
            if replayed.meta is None:
                raise ResidencyStoreError("Residency Session stream has no identity fact")
            if self._session_log(session_id).committed_version < record.source_session_revision:
                raise ResidencyStoreError("Residency source Session revision is unavailable")
            agent.restore_residency_history(tuple(replayed.model_context_messages), replayed.meta)
            agent.restore_residency_message_buffer(record.message_buffer_snapshot)
            mailbox = Mailbox(session_id)
            install_fence = ResidencyFence(lease.subject, lease.owner_id, lease.fencing_token)
            if record.lifecycle is ResidencyLifecycle.INSTALLING:
                if record.install_fence is None:
                    raise ResidencyStoreError("Residency install claim is invalid")
                if record.install_fence.subject != lease.subject:
                    raise ResidencyStoreError("Residency install claim subject mismatch")
                if record.install_fence.fencing_token > lease.fencing_token:
                    raise ResidencyStoreError("Residency install claim has a newer owner")
            claimed = replace(
                record,
                record_revision=record.record_revision + 1,
                lifecycle=ResidencyLifecycle.INSTALLING,
                install_fence=install_fence,
            )
            claimed_data = encode_residency_record(claimed)
            self._writer.flush_inline()
            atomic_write(self._path(session_id), claimed_data)
            return RehydratedResidency(agent, mailbox, claimed.record_revision)

    def forget(
        self,
        session_id: str,
        *,
        expected_record_revision: int,
        lease: LeaseEpoch,
    ) -> bool:
        with self._locked(session_id):
            self._lease_coordinator.assert_current(lease.subject, lease.fencing_token)
            record = self._read_unlocked(session_id)
            if record is None:
                return False
            if record.record_revision != expected_record_revision:
                raise ResidencyStoreError("Residency forget revision conflict")
            if record.materialization_fence.subject != lease.subject:
                raise ResidencyStoreError("Residency forget lease subject mismatch")
            if record.lifecycle is not ResidencyLifecycle.INSTALLING:
                raise ResidencyStoreError("Residency forget requires an install claim")
            if (
                record.install_fence is None
                or record.install_fence.owner_id != lease.owner_id
                or record.install_fence.fencing_token != lease.fencing_token
            ):
                raise ResidencyStoreError("Residency forget install owner mismatch")
            self._path(session_id).unlink()
            return True

    def _read_unlocked(self, session_id: str) -> ResidencyRecord | None:
        try:
            data = self._path(session_id).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ResidencyStoreError("Residency record cannot be read") from exc
        try:
            return decode_residency_record(data, expected_agent_id=session_id)
        except (TypeError, ValueError) as exc:
            raise ResidencyStoreError("Residency record is invalid") from exc

    @staticmethod
    def _validate_agent_identity(agent: ResidentAgentStatePort, identity: ResidencyIdentity) -> None:
        if agent.session_id != identity.logical_agent_id:
            raise ResidencyStoreError("Resident Agent logical identity mismatch")
        if agent.residency_definition_id != identity.definition_id:
            raise ResidencyStoreError("Resident Agent definition identity mismatch")
        if agent.residency_config_digest != identity.config_digest:
            raise ResidencyStoreError("Resident Agent configuration identity mismatch")

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        lock_path = self._lock_path(session_id)
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+b")
        except OSError as exc:
            raise ResidencyStoreError("Residency lock cannot be opened") from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except ResidencyStoreError:
                raise
            except OSError as exc:
                raise ResidencyStoreError("Residency lock operation failed") from exc


__all__ = ["RehydratedResidency", "ResidencyStore", "ResidencyStoreError"]
