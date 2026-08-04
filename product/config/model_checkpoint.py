"""Approved Product profile for durable ModelCall bounds."""

from mote.contracts.model.checkpoint import ModelCheckpointPolicy


def approved_model_checkpoint_policy() -> ModelCheckpointPolicy:
    return ModelCheckpointPolicy(
        schema_version=1,
        active_per_session=100,
        active_global=1_000,
        inline_response_bytes=64 * 1024,
        frame_bytes=2 * 1024 * 1024,
        reconcile_batch=200,
        reconcile_seconds=5,
        stream_soft_bytes=64 * 1024 * 1024,
        stream_hard_bytes=256 * 1024 * 1024,
        compaction_identities=1_000,
        compaction_candidate_bytes=64 * 1024 * 1024,
        compaction_seconds=5,
        terminal_retention_days=90,
        tombstone_retention_days=365,
    )


__all__ = ["approved_model_checkpoint_policy"]
