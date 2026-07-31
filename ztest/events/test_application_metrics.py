from mote.contracts.events.application import (
    ApplicationActivationCommitted,
    CompositionCloseFailed,
    InferenceTargetCapacityReached,
)
from mote.runtime.telemetry.application_metrics import ApplicationMetricsProjection


def test_application_metrics_are_low_cardinality_and_redacted():
    canary = "canary-secret-9dc9"
    projection = ApplicationMetricsProjection()
    projection.observe(
        ApplicationActivationCommitted(
            application_generation_id=f"generation-{canary}",
            runtime_generation_id=f"runtime-{canary}",
            topology_revision=f"revision-{canary}",
            source_revision=f"source-{canary}",
        )
    )
    projection.observe(
        CompositionCloseFailed(
            resource_kind="credential_lease",
            resource_identity=f"identity-{canary}",
            error_code="CLOSE_FAILED",
            error_count=1,
        )
    )
    projection.observe(InferenceTargetCapacityReached(target_count=1024, limit=1024))

    snapshot = projection.snapshot()
    rendered = repr(snapshot)
    assert canary not in rendered
    assert "generation-" not in rendered
    assert "identity-" not in rendered
    assert snapshot.inference_target_count == 1024
