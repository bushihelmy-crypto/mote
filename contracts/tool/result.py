"""Structured facts shared by tool results and tool events."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.artifact import ArtifactRef


@dataclass
class ToolMedia:
    artifact: ArtifactRef
    kind: str = "image"
    ref: str = ""
    mime: str | None = None


@dataclass
class FileChange:
    path: str = ""
    old: str = ""
    new: str = ""
    transaction_id: str = ""
    post_digest: str = ""


__all__ = ["FileChange", "ToolMedia"]
