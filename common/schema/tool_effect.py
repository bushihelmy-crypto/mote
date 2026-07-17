"""ToolEffect — a tool's side-effect class, used to decide crash-replay safety.

A single axis orthogonal to permissions: *where does a tool's effect land, and
is that effect recoverable if the run is interrupted mid-call?*

- ``PURE``     — no side effect (Read/Grep/Glob/LS). Re-running is always safe;
                 the result is re-derivable. The effect ledger ignores these.
- ``LOCAL``    — mutates only local, recoverable state (the filesystem). Already
                 protected by the ``FileSnapshotRecorder`` (before-image blobs),
                 so re-running an interrupted call is safe. Ledger ignores these.
- ``EXTERNAL`` — the effect escapes the locally-recoverable boundary (network,
                 IPC, a subprocess, a human-visible action, a spawned agent, an
                 MCP server). There is no before-image and replay may duplicate
                 the effect, so these are the ONLY calls the effect ledger tracks
                 (started/completed/failed) and the ONLY ones guarded against
                 blind re-execution after a crash.

A tool's default is *derived* from its existing metadata (see
``BaseTool.effect``): read-only tools → PURE, filesystem-mutating tools → LOCAL,
everything else → EXTERNAL (conservative: an untagged tool with an unknown
effect is guarded, not silently replayed). A tool overrides the class attribute
only when the derivation is wrong for it.

Lives in ``common.schema`` as a pure leaf enum (like ``node_status``) so every
layer — the tool base class, the executor chokepoint, and the resume/replay
reconciler — imports it without a dependency cycle.
"""

from __future__ import annotations

from enum import Enum


class ToolEffect(str, Enum):
    """Where a tool's side effect lands and whether replay is safe.

    A ``str`` enum so the value serializes directly into the ledger records and
    can be compared against a stored string without conversion.
    """

    PURE = "pure"
    LOCAL = "local"
    EXTERNAL = "external"


__all__ = ["ToolEffect"]
