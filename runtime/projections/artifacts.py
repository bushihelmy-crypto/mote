"""Shared materialization of exported bytes into Artifact publication intents."""
from __future__ import annotations

import hashlib
import json

from mote.contracts.artifacts import (
    ArtifactPublicationIntent,
    ArtifactRepresentationInput,
    ArtifactRepresentationIntent,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.ports import ArtifactBlobStore
from mote.contracts.runtimes import RuntimeProjectionIntent
from mote.runtime.disk.async_io import run_disk_io

_ARTIFACT_POLICY_OPTIONS = frozenset({"retention", "sensitivity"})


def artifact_representation_set_digest(
    representations: tuple[ArtifactRepresentationInput, ...],
) -> str:
    """Canonical digest for one complete set of exported representations."""
    identity = [
        {
            "representation": item.representation,
            "mime_type": item.mime_type,
            "digest": hashlib.sha256(item.content).hexdigest(),
            "suggested_name": item.suggested_name,
        }
        for item in sorted(
            representations,
            key=lambda item: item.representation,
        )
    ]
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def artifact_projection_policy(
    intent: RuntimeProjectionIntent,
    *,
    allowed_options: frozenset[str] = frozenset(),
) -> tuple[ArtifactRetention, ArtifactSensitivity]:
    """Parse the shared Artifact lifecycle options for one schema-versioned intent."""
    options = dict(intent.options)
    unknown = set(options) - _ARTIFACT_POLICY_OPTIONS - allowed_options
    if unknown:
        raise ValueError("unsupported artifact projection options: " + ", ".join(sorted(unknown)))
    return (
        ArtifactRetention(options.get("retention", ArtifactRetention.SESSION.value)),
        ArtifactSensitivity(options.get("sensitivity", ArtifactSensitivity.PRIVATE.value)),
    )


async def materialize_artifact_projection(
    blob_store: ArtifactBlobStore,
    representations: tuple[ArtifactRepresentationInput, ...],
    *,
    identity_representation: str,
    artifact_prefix: str,
    artifact_digest: str = "",
    retention: ArtifactRetention = ArtifactRetention.SESSION,
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.PRIVATE,
) -> ArtifactPublicationIntent:
    """Seal exported bytes in trusted CAS and return an idempotent publication."""
    if not representations:
        raise ValueError("artifact projection requires at least one representation")
    by_name = {item.representation: item for item in representations}
    if len(by_name) != len(representations):
        raise ValueError("artifact projection representation names must be unique")
    try:
        identity = by_name[identity_representation]
    except KeyError as exc:
        raise ValueError("artifact projection identity representation is missing") from exc
    state_digest = artifact_digest or hashlib.sha256(identity.content).hexdigest()
    artifact_id = f"{artifact_prefix}-{state_digest}"
    materialized = []
    for representation in representations:
        content_ref = await run_disk_io(
            blob_store.put_bytes,
            representation.content,
        )
        materialized.append(
            ArtifactRepresentationIntent(
                representation=representation.representation,
                kind=representation.kind,
                mime_type=representation.mime_type,
                content=content_ref,
                suggested_name=representation.suggested_name,
            )
        )
    return ArtifactPublicationIntent(
        publication_id=artifact_id,
        artifact_id=artifact_id,
        expected_revision=0,
        retention=retention,
        sensitivity=sensitivity,
        representations=tuple(materialized),
    )


__all__ = [
    "artifact_projection_policy",
    "artifact_representation_set_digest",
    "materialize_artifact_projection",
]
