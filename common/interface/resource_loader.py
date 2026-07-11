"""ResourceProvider protocol — the resource-registration slice.

The narrow face the Skill tool uses to register a loaded capability body into the
Role's ResourceRegistry (so it survives history compaction) without importing the
concrete ``roles`` implementation.

Why a Protocol here (not a direct import): the ``executor`` layer must never
import the ``roles`` layer (the strict downward-only layering rule). The concrete
``ResourceRegistry`` lives in ``common.resource`` and is wired into a Role
capability (``register_resource``) that the tool receives via injection; the tool
only depends on this structural face, so no upward import is introduced.

Like the other ``common.interface`` Protocols (e.g.
:class:`~mote.common.interface.FileSnapshotStore`), this is a leaf module that
only needs ``typing``, importable from anywhere without risking a cycle.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ResourceProvider(Protocol):
    """Registers a loaded capability body for post-compaction re-projection.

    Implemented by the Role capability that delegates to
    ``common.resource.ResourceRegistry.load`` (production) and any test double.
    Called by the Skill tool right after it renders a skill body inline, so the
    body is re-projected after the head is compacted away. Must be cheap and
    non-throwing from the tool's point of view.
    """

    def register_resource(self, *, id: str, kind: str, content: str) -> None:
        """Register a loaded resource body under a stable id (last-write-wins).

        Args:
            id: Stable identity of the resource (e.g. the skill name).
            kind: Resource category ("skill", ...).
            content: The rendered body to preserve across compaction.
        """
        ...
