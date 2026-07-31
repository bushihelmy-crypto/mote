"""Application-level atomic gateway generation owner."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from mote.contracts.inference.generation_artifact import GenerationArtifact


class GenerationState(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    DRAINING = "draining"
    RETIRED = "retired"
    REJECTED = "rejected"


class GenerationDomain(StrEnum):
    MODEL = "model"
    SERVICE = "service"
    SESSION = "session"
    TRANSFER = "transfer"


@dataclass(frozen=True, slots=True)
class GenerationView:
    generation_id: str
    artifact_digest: str
    domain: GenerationDomain
    bindings: Mapping[str, Any]
    transport_registry_revision: str
    client_profile_revision: str
    failure_policy_revision: str


@dataclass
class _GenerationRecord:
    artifact: GenerationArtifact
    state: GenerationState
    references: dict[GenerationDomain, int]


class GatewayGenerationLease:
    def __init__(
        self,
        owner: "GatewayGenerationOwner",
        generation_id: str,
        domain: GenerationDomain,
        view: GenerationView,
    ) -> None:
        self._owner = owner
        self.generation_id = generation_id
        self.artifact_digest = view.artifact_digest
        self.domain = domain
        self._view = view
        self._released = False

    def view(self) -> GenerationView:
        if self._released:
            raise RuntimeError("generation lease already released")
        return self._view

    def model_view(self) -> GenerationView:
        return self._require(GenerationDomain.MODEL)

    def service_view(self) -> GenerationView:
        return self._require(GenerationDomain.SERVICE)

    def session_view(self) -> GenerationView:
        return self._require(GenerationDomain.SESSION)

    def transfer_view(self) -> GenerationView:
        return self._require(GenerationDomain.TRANSFER)

    def release(self) -> None:
        if self._released:
            raise RuntimeError("generation lease already released")
        self._released = True
        self._owner._release(self.generation_id, self.domain)

    def __enter__(self) -> "GatewayGenerationLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def _require(self, domain: GenerationDomain) -> GenerationView:
        if self.domain is not domain:
            raise PermissionError(f"{self.domain.value} lease cannot access {domain.value} view")
        return self.view()


class GatewayGenerationOwner:
    """The sole activation authority; gateways receive only pinned narrow leases."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, _GenerationRecord] = {}
        self._active_id: str | None = None

    @property
    def active_generation_id(self) -> str | None:
        with self._lock:
            return self._active_id

    def stage(self, artifact: GenerationArtifact) -> None:
        with self._lock:
            existing = self._records.get(artifact.generation_id)
            if existing is not None:
                if existing.artifact.artifact_digest != artifact.artifact_digest:
                    raise ValueError("generation id reused with a different artifact digest")
                return
            self._records[artifact.generation_id] = _GenerationRecord(
                artifact=artifact,
                state=GenerationState.STAGED,
                references={domain: 0 for domain in GenerationDomain},
            )

    def restore(self, records: Iterable[tuple[GenerationArtifact, GenerationState]]) -> None:
        restored = tuple(records)
        active = tuple(artifact.generation_id for artifact, state in restored if state is GenerationState.ACTIVE)
        if len(active) > 1:
            raise ValueError("generation recovery found multiple active generations")
        with self._lock:
            if self._records:
                raise RuntimeError("generation recovery requires an empty owner")
            for artifact, state in restored:
                if artifact.generation_id in self._records:
                    raise ValueError("generation recovery found duplicate identity")
                self._records[artifact.generation_id] = _GenerationRecord(
                    artifact=artifact,
                    state=state,
                    references={domain: 0 for domain in GenerationDomain},
                )
            self._active_id = active[0] if active else None

    def reject(self, generation_id: str) -> None:
        with self._lock:
            record = self._require(generation_id)
            if record.state is not GenerationState.STAGED:
                raise ValueError("only staged generation may be rejected")
            record.state = GenerationState.REJECTED

    def activate(self, generation_id: str, artifact_digest: str) -> None:
        with self._lock:
            candidate = self._require(generation_id)
            if candidate.artifact.artifact_digest != artifact_digest:
                raise ValueError("generation artifact digest mismatch")
            if candidate.state is not GenerationState.STAGED:
                raise ValueError(f"generation cannot activate from {candidate.state.value}")
            if self._active_id is not None and self._active_id != generation_id:
                current = self._require(self._active_id)
                current.state = GenerationState.DRAINING
            candidate.state = GenerationState.ACTIVE
            self._active_id = generation_id

    def acquire(self, domain: GenerationDomain) -> GatewayGenerationLease:
        with self._lock:
            if self._active_id is None:
                raise RuntimeError("no active gateway generation")
            record = self._require(self._active_id)
            if record.state is not GenerationState.ACTIVE:
                raise RuntimeError("active generation is not ready")
            record.references[domain] += 1
            return GatewayGenerationLease(
                self,
                record.artifact.generation_id,
                domain,
                self._view(record.artifact, domain),
            )

    def state(self, generation_id: str) -> GenerationState:
        with self._lock:
            return self._require(generation_id).state

    def describe(self, generation_id: str) -> tuple[str, GenerationState]:
        with self._lock:
            record = self._require(generation_id)
            return record.artifact.artifact_digest, record.state

    def references(self, generation_id: str) -> Mapping[GenerationDomain, int]:
        with self._lock:
            return MappingProxyType(dict(self._require(generation_id).references))

    def _release(self, generation_id: str, domain: GenerationDomain) -> None:
        with self._lock:
            record = self._require(generation_id)
            if record.references[domain] <= 0:
                raise RuntimeError("generation reference underflow")
            record.references[domain] -= 1
            if record.state is GenerationState.DRAINING and not any(record.references.values()):
                record.state = GenerationState.RETIRED

    def _require(self, generation_id: str) -> _GenerationRecord:
        try:
            return self._records[generation_id]
        except KeyError as exc:
            raise KeyError(f"unknown generation {generation_id}") from exc

    @staticmethod
    def _view(artifact: GenerationArtifact, domain: GenerationDomain) -> GenerationView:
        bindings = {
            GenerationDomain.MODEL: artifact.model_planner_and_bindings,
            GenerationDomain.SERVICE: artifact.service_planner_and_bindings,
            GenerationDomain.SESSION: artifact.session_capability_and_bindings,
            GenerationDomain.TRANSFER: artifact.transfer_capability_and_bindings,
        }[domain]
        return GenerationView(
            generation_id=artifact.generation_id,
            artifact_digest=artifact.artifact_digest,
            domain=domain,
            bindings=MappingProxyType(dict(bindings)),
            transport_registry_revision=artifact.transport_registry_revision,
            client_profile_revision=artifact.client_profile_revision,
            failure_policy_revision=artifact.failure_policy_revision,
        )
