from __future__ import annotations

import dataclasses
import hashlib

import pytest

from mote.contracts.execution.pending_act import ToolCompositionDefinitionRef
from mote.runtime.tools.snapshots import RuntimeToolSnapshotManager
from ztest.executor.test_tool_snapshots import Executor, target


def _definition(snapshot) -> ToolCompositionDefinitionRef:
    return ToolCompositionDefinitionRef(
        snapshot.catalog.identity.catalog_id,
        snapshot.catalog.identity.version,
        snapshot.catalog.fingerprint,
        snapshot.composition_generation_id,
        snapshot.catalog.fingerprint,
        f"sha256-{hashlib.sha256(snapshot.provider_descriptor.encode()).hexdigest()}",
        snapshot.composition_generation_id,
        snapshot.capability_fingerprint,
    )


def test_snapshot_restore_pins_an_exactly_matching_candidate() -> None:
    manager = RuntimeToolSnapshotManager(Executor(), composition_generation_id="application-generation-1")
    original = manager.materialize(target(), include_hidden=False)
    definition = _definition(original)
    manager.release(original)

    restored = manager.restore(definition, target(), include_hidden=False)

    assert restored.catalog.fingerprint == definition.catalog_fingerprint
    assert restored.composition_generation_id == definition.composition_generation_id


def test_snapshot_restore_releases_and_fails_closed_on_generation_mismatch() -> None:
    manager = RuntimeToolSnapshotManager(Executor(), composition_generation_id="application-generation-1")
    original = manager.materialize(target(), include_hidden=False)
    definition = dataclasses.replace(_definition(original), composition_generation_id="other")
    manager.release(original)

    with pytest.raises(ValueError, match="cannot be reconstructed exactly"):
        manager.restore(definition, target(), include_hidden=False)
