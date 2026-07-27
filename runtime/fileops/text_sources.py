"""Single text-materialization path for Read and Search."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Optional

from mote.contracts.fileops.errors import DocumentExtractionError, FileBinaryContentError, FileReadRangeError
from mote.contracts.fileops.models import EncodingSource, ExtractionBudget, FileSnapshot, TextViewMode
from mote.runtime.fileops.artifact_repository import ArtifactRepository, ArtifactWriteScope
from mote.runtime.fileops.capture import ManagedSnapshotCapture
from mote.runtime.fileops.document_budgets import DEFAULT_EXTRACTION_BUDGET
from mote.runtime.fileops.documents import extract_document_bytes, is_document
from mote.runtime.fileops.encoding import decode_text
from mote.runtime.fileops.identity import PathLike


@dataclass(frozen=True)
class MaterializedText:
    snapshot: FileSnapshot
    mode: TextViewMode
    text: str


class TextSourceService:
    """Materializes exactly one sealed file version into auditable text."""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        capture: ManagedSnapshotCapture,
        extraction_budget: ExtractionBudget = DEFAULT_EXTRACTION_BUDGET,
    ) -> None:
        self.artifacts = artifacts
        self.capture = capture
        self.extraction_budget = extraction_budget

    def materialize(
        self,
        path: PathLike,
        *,
        scope: ArtifactWriteScope,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        maximum_bytes: Optional[int] = None,
    ) -> MaterializedText:
        snapshot = self.capture.capture(path, scope=scope)
        if maximum_bytes is not None and snapshot.version.size > maximum_bytes:
            raise FileReadRangeError(
                f"text source exceeds {maximum_bytes} bytes",
                size=snapshot.version.size,
                maximum=maximum_bytes,
            )
        raw = self.artifacts.read_bytes(snapshot.artifact)
        display_path = snapshot.requested_path.display
        if is_document(display_path):
            try:
                text = extract_document_bytes(
                    raw,
                    os.path.splitext(display_path)[1],
                    budget=self.extraction_budget,
                )
            except DocumentExtractionError:
                raise
            except Exception as exc:
                raise DocumentExtractionError(
                    f"cannot extract document text: {exc}",
                    path=display_path,
                    cause=exc,
                ) from exc
            return MaterializedText(
                snapshot=snapshot,
                mode=TextViewMode.DOCUMENT,
                text=text,
            )

        text, decision = decode_text(
            raw,
            explicit=encoding,
            fallback=fallback_encoding,
        )
        if b"\0" in raw and encoding is None and decision.source != EncodingSource.BOM:
            raise FileBinaryContentError(
                "file contains NUL bytes without a text BOM or explicit encoding",
                path=display_path,
            )
        snapshot = replace(snapshot, encoding=decision)
        return MaterializedText(
            snapshot=snapshot,
            mode=TextViewMode.TEXT,
            text=text,
        )


__all__ = ["MaterializedText", "TextSourceService"]
