"""Artifact projection reconstructed from a Notebook-bearing checkpoint."""
from __future__ import annotations

from mote.contracts.artifacts import ArtifactPublicationIntent, ArtifactRepresentationInput
from mote.contracts.notebook import NotebookDocument
from mote.contracts.ports import ArtifactBlobStore
from mote.contracts.runtimes import RuntimeProjectionRequest
from mote.runtime.interactive.checkpoint_codec import decode_inline_json
from mote.runtime.projections.artifacts import artifact_projection_policy, materialize_artifact_projection
from mote.runtime.tools.dependency.notebook_export import export_notebook_ipynb


class NotebookArtifactProjector:
    """Rebuild deterministic `.ipynb` bytes without starting a Kernel or GUI."""

    projector = "notebook-artifact"
    schema_version = 1

    def __init__(self, blob_store: ArtifactBlobStore) -> None:
        self._blob_store = blob_store

    async def project(self, request: RuntimeProjectionRequest) -> ArtifactPublicationIntent:
        checkpoint = request.checkpoint
        if checkpoint.kind != "jupyter":
            raise ValueError("notebook projector requires a jupyter checkpoint")
        payload = decode_inline_json(
            checkpoint,
            codec="jupyter-state+json@2",
        )
        if not isinstance(payload, dict) or "notebook" not in payload:
            raise ValueError("jupyter checkpoint does not contain a notebook snapshot")
        document = NotebookDocument.model_validate(payload["notebook"])
        exported = export_notebook_ipynb(document)
        retention, sensitivity = artifact_projection_policy(request.intent)
        return await materialize_artifact_projection(
            self._blob_store,
            (
                ArtifactRepresentationInput(
                    representation=exported.representation,
                    kind="notebook",
                    mime_type=exported.mime_type,
                    content=exported.content,
                    suggested_name=exported.suggested_name,
                ),
            ),
            identity_representation="ipynb",
            artifact_prefix="notebook",
            retention=retention,
            sensitivity=sensitivity,
        )


__all__ = ["NotebookArtifactProjector"]
