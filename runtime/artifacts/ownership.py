"""Logical Artifact ownership inside the workspace-wide catalog."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.artifacts import ArtifactRetention

_GLOBAL_OWNER_ID = "global"


@dataclass(frozen=True, slots=True)
class ArtifactOwnership:
    """Bind one lightweight store facade to its visible lifecycle owners."""

    session_id: str
    project_id: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("artifact ownership requires a session_id")
        if not self.project_id:
            raise ValueError("artifact ownership requires a project_id")

    @classmethod
    def standalone(cls) -> "ArtifactOwnership":
        return cls(session_id="standalone", project_id="standalone")

    def owner_for(self, retention: ArtifactRetention) -> tuple[str, str]:
        retention = ArtifactRetention(retention)
        if retention in {ArtifactRetention.EPHEMERAL, ArtifactRetention.SESSION}:
            return "session", self.session_id
        if retention is ArtifactRetention.PROJECT:
            return "project", self.project_id
        return "global", _GLOBAL_OWNER_ID

    def visible_owners(self) -> tuple[tuple[str, str], ...]:
        return (
            ("session", self.session_id),
            ("project", self.project_id),
            ("global", _GLOBAL_OWNER_ID),
        )


__all__ = ["ArtifactOwnership"]
