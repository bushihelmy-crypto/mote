"""Durable generation, migration debt, and restore-admission closure."""

from datetime import datetime, timezone

import pytest

from mote.contracts.events.governance import (
    CodecState,
    CutoverDeclaration,
    CutoverMode,
    CutoverState,
    CutoverTransition,
)
from mote.product.composition.event_governance import TRANSFORMATION_DECLARATIONS
from mote.product.composition.store_governance import (
    ACTIVE_STORE_DECLARATIONS,
    ARCHIVE_CAPABILITY_DECLARATIONS,
    MIGRATION_DEBT_DECLARATIONS,
    RESTORE_COPY_DECLARATIONS,
)
from mote.product.inference.daemon.operations_audit_codec import OPERATIONS_AUDIT_ACTIVE_CODEC
from mote.runtime.events.cutover import validate_cutover_history
from mote.runtime.session.codec import SESSION_ACTIVE_CODECS


def test_online_codecs_have_one_active_generation_and_complete_policy() -> None:
    codecs = (*SESSION_ACTIVE_CODECS, OPERATIONS_AUDIT_ACTIVE_CODEC)
    keys = [(entry.logical_store, entry.event_family) for entry in codecs]
    assert len(keys) == len(set(keys))
    assert all(entry.state is CodecState.ACTIVE for entry in codecs)
    assert all(entry.policy.retention_requirement for entry in codecs)
    assert all(entry.policy.legal_hold_behavior for entry in codecs)
    assert all(entry.policy.secondary_copy_policy for entry in codecs)


def test_active_store_declarations_cover_codec_catalog_exactly() -> None:
    codec_keys = {
        (entry.logical_store, entry.event_family, entry.store_generation)
        for entry in (*SESSION_ACTIVE_CODECS, OPERATIONS_AUDIT_ACTIVE_CODEC)
    }
    declared_keys = {
        (store.logical_store, family, store.active_generation)
        for store in ACTIVE_STORE_DECLARATIONS
        if store.logical_store != "inference-gateway-authority"
        for family in store.included_event_families
    }
    assert codec_keys == declared_keys
    assert len({store.cutover_unit_id for store in ACTIVE_STORE_DECLARATIONS}) == len(ACTIVE_STORE_DECLARATIONS)
    gateway = next(store for store in ACTIVE_STORE_DECLARATIONS if store.logical_store == "inference-gateway-authority")
    assert gateway.restore_admission == "mote.product.inference.restore.IsolatedSQLiteRestoreService.apply"


def test_final_online_state_has_no_migration_or_restore_copy_debt() -> None:
    assert MIGRATION_DEBT_DECLARATIONS == ()
    assert RESTORE_COPY_DECLARATIONS == ()
    assert len(ARCHIVE_CAPABILITY_DECLARATIONS) == 1
    assert ARCHIVE_CAPABILITY_DECLARATIONS[0].archival_generation is None
    assert ARCHIVE_CAPABILITY_DECLARATIONS[0].online_archive_reader == ""


def test_each_codec_has_declared_encode_and_decode_transformations() -> None:
    converters = {item.converter for item in TRANSFORMATION_DECLARATIONS}
    assert "mote.runtime.session.codec.encode_session_event" in converters
    assert "mote.runtime.session.codec.decode_session_event" in converters
    assert "mote.product.inference.daemon.operations_audit_codec.encode_operations_audit_event" in converters
    assert "mote.product.inference.daemon.operations_audit_codec.decode_operations_audit_event" in converters


def test_cutover_history_is_forward_only_and_evidence_bound() -> None:
    declaration = CutoverDeclaration(
        cutover_unit_id="test-cutover",
        logical_store="test-store",
        included_event_families=("test-event",),
        source_generation=1,
        target_generation=2,
        mode=CutoverMode.OFFLINE_CUTOVER,
        shared_sequence_domain="sequence",
        shared_checksum_domain="checksum",
        shared_checkpoint_domain="checkpoint",
        transaction_boundary="transaction",
        writer_fence="persistent CAS",
        lease_quiesce_policy="all leases expire",
        activation_record="generation record",
        forward_recovery_owner="store-owner",
        cleanup_prerequisite="all copies admitted",
        max_write_unavailable_seconds=30,
        drain_deadline_seconds=30,
    )
    occurred_at = datetime(2026, 7, 31, tzinfo=timezone.utc)
    fenced = CutoverTransition(
        previous=CutoverState.PREPARED,
        next=CutoverState.WRITER_FENCED,
        expected_activation_generation=2,
        cas_revision=1,
        actor="operator",
        owner_id="store-owner",
        occurred_at=occurred_at,
        prerequisite_evidence_digests=("sha256:" + "1" * 64,),
    )
    assert validate_cutover_history(declaration, (fenced,)) is CutoverState.WRITER_FENCED
    rollback = CutoverTransition(
        previous=CutoverState.WRITER_FENCED,
        next=CutoverState.PREPARED,
        expected_activation_generation=2,
        cas_revision=2,
        actor="operator",
        owner_id="store-owner",
        occurred_at=occurred_at,
        prerequisite_evidence_digests=("sha256:" + "2" * 64,),
    )
    with pytest.raises(ValueError, match="illegal cutover transition"):
        validate_cutover_history(declaration, (fenced, rollback))
