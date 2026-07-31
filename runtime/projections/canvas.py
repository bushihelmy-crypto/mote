"""Artifact projection reconstructed from a full Canvas checkpoint."""
from __future__ import annotations

from mote.contracts.artifact import ArtifactPublicationIntent, ArtifactRepresentationInput
from mote.contracts.ports.artifact.store import ArtifactBlobStore
from mote.contracts.ports.surface.canvas_backend import CanvasExportPort
from mote.contracts.runtime import RuntimeProjectionRequest
from mote.contracts.surface import CanvasDocument
from mote.runtime.interactive.canvas.export import CanvasExportService
from mote.runtime.interactive.checkpoint_codec import decode_inline_json
from mote.runtime.projections.artifacts import (
    artifact_projection_policy,
    artifact_representation_set_digest,
    materialize_artifact_projection,
)


class CanvasArtifactProjector:
    """Rebuild the deterministic native Canvas export without a live window."""

    projector = "canvas-artifact"
    schema_version = 1

    def __init__(
        self,
        blob_store: ArtifactBlobStore,
        export_service: CanvasExportPort | None = None,
    ) -> None:
        self._blob_store = blob_store
        self._export_service = export_service or CanvasExportService()

    async def project(self, request: RuntimeProjectionRequest) -> ArtifactPublicationIntent:
        checkpoint = request.checkpoint
        if checkpoint.kind != "canvas":
            raise ValueError("canvas projector requires a canvas checkpoint")
        payload = decode_inline_json(
            checkpoint,
            codec="canvas-document+json@1",
        )
        document = CanvasDocument.model_validate(payload)
        options = dict(request.intent.options)
        formats = tuple(item for item in options.get("formats", "svg").split(",") if item)
        exports = await self._export_service.export(document, formats)
        representations = tuple(
            ArtifactRepresentationInput(
                representation=export.representation,
                kind="canvas",
                mime_type=export.mime_type,
                content=export.content,
                suggested_name=export.suggested_name,
            )
            for export in exports
        )
        retention, sensitivity = artifact_projection_policy(
            request.intent,
            allowed_options=frozenset({"formats"}),
        )
        return await materialize_artifact_projection(
            self._blob_store,
            representations,
            identity_representation="svg",
            artifact_prefix="canvas",
            artifact_digest=artifact_representation_set_digest(representations),
            retention=retention,
            sensitivity=sensitivity,
        )


__all__ = ["CanvasArtifactProjector"]
