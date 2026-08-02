"""Canonical Runtime owner for transient Artifact reachability pins."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence
from uuid import uuid4

from mote.contracts.artifact import ArtifactContentRef

from .ports import ArtifactPinSource


@dataclass(frozen=True, slots=True)
class ArtifactPinSnapshot:
    generation: int
    artifacts: tuple[ArtifactContentRef, ...]


@dataclass(frozen=True, slots=True)
class ArtifactPinReceipt:
    pin_id: str
    producer_id: str
    generation: int


class ArtifactPinRegistry:
    """One typed registry consumed by every collector in a workspace runtime.

    Producers either register a source whose own freeze lease protects its
    snapshot, or acquire a direct pin for an operation lifetime.  Collection
    holds the registry lock and every source lease until reclaim completes, so
    a stale snapshot cannot race a new direct pin or source registration.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 1
        self._sources: dict[str, ArtifactPinSource] = {}
        self._direct: dict[str, tuple[str, tuple[ArtifactContentRef, ...], int]] = {}

    def register_source(self, producer_id: str, source: ArtifactPinSource) -> None:
        if type(producer_id) is not str or not producer_id:
            raise ValueError("artifact pin producer identity is required")
        if not isinstance(source, ArtifactPinSource):
            raise TypeError("artifact pin source does not implement the canonical Port")
        with self._lock:
            if producer_id in self._sources:
                raise ValueError(f"artifact pin producer {producer_id!r} is already registered")
            self._sources[producer_id] = source
            self._generation += 1

    def acquire(self, producer_id: str, artifacts: Sequence[ArtifactContentRef]) -> ArtifactPinReceipt:
        refs = _canonical_refs(artifacts)
        if type(producer_id) is not str or not producer_id:
            raise ValueError("artifact pin producer identity is required")
        if not refs:
            raise ValueError("artifact pin must protect at least one object")
        with self._lock:
            self._generation += 1
            receipt = ArtifactPinReceipt(uuid4().hex, producer_id, self._generation)
            self._direct[receipt.pin_id] = (producer_id, refs, receipt.generation)
            return receipt

    def release(self, receipt: ArtifactPinReceipt) -> bool:
        with self._lock:
            current = self._direct.get(receipt.pin_id)
            if current is None:
                return False
            if current[0] != receipt.producer_id or current[2] != receipt.generation:
                raise RuntimeError("artifact pin receipt is stale or foreign")
            del self._direct[receipt.pin_id]
            self._generation += 1
            return True

    @contextmanager
    def freeze_artifact_pins(self) -> Iterator[tuple[ArtifactContentRef, ...]]:
        with self._lock:
            with ExitStack() as stack:
                refs = [ref for _, items, _ in self._direct.values() for ref in items]
                for producer_id in sorted(self._sources):
                    refs.extend(stack.enter_context(self._sources[producer_id].freeze_artifact_pins()))
                yield _canonical_refs(refs)

    def snapshot(self) -> ArtifactPinSnapshot:
        with self.freeze_artifact_pins() as refs:
            return ArtifactPinSnapshot(self._generation, refs)


def _canonical_refs(refs: Sequence[ArtifactContentRef]) -> tuple[ArtifactContentRef, ...]:
    by_digest: dict[str, ArtifactContentRef] = {}
    for ref in refs:
        if not isinstance(ref, ArtifactContentRef):
            raise TypeError("artifact pin contains a non-canonical reference")
        prior = by_digest.setdefault(ref.identity.digest, ref)
        if prior.identity.size != ref.identity.size:
            raise ValueError("artifact pin digest resolves to conflicting sizes")
    return tuple(by_digest[digest] for digest in sorted(by_digest))


__all__ = ["ArtifactPinReceipt", "ArtifactPinRegistry", "ArtifactPinSnapshot"]
