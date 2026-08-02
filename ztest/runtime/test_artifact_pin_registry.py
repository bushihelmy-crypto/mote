from __future__ import annotations

import threading

import pytest

from mote.contracts.artifact import ArtifactContentRef, ContentLocator
from mote.contracts.content.identity import ContentIdentity
from mote.runtime.artifacts.pins import ArtifactPinRegistry


def _ref(digest: str, size: int = 1) -> ArtifactContentRef:
    return ArtifactContentRef(ContentIdentity(digest, size), ContentLocator(f"sha256:{digest}"))


def test_direct_pin_is_revisioned_idempotently_released() -> None:
    registry = ArtifactPinRegistry()
    receipt = registry.acquire("publication:one", (_ref("a" * 64),))
    assert registry.snapshot().artifacts == (_ref("a" * 64),)
    assert registry.release(receipt)
    assert not registry.release(receipt)
    assert registry.snapshot().artifacts == ()


def test_foreign_or_stale_receipt_fails_closed() -> None:
    registry = ArtifactPinRegistry()
    receipt = registry.acquire("cursor:one", (_ref("b" * 64),))
    foreign = type(receipt)(receipt.pin_id, "cursor:other", receipt.generation)
    with pytest.raises(RuntimeError, match="stale or foreign"):
        registry.release(foreign)


def test_frozen_snapshot_blocks_new_pin_until_collector_releases() -> None:
    registry = ArtifactPinRegistry()
    entered = threading.Event()
    acquired = threading.Event()

    def producer() -> None:
        entered.set()
        registry.acquire("stage:one", (_ref("c" * 64),))
        acquired.set()

    with registry.freeze_artifact_pins() as snapshot:
        assert snapshot == ()
        thread = threading.Thread(target=producer)
        thread.start()
        entered.wait(timeout=1)
        assert not acquired.wait(timeout=0.05)
    thread.join(timeout=1)
    assert acquired.is_set()
    assert registry.snapshot().artifacts == (_ref("c" * 64),)
