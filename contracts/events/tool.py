"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Literal, Optional

from mote.contracts.tool.identity import ToolInvocationIdentity

if TYPE_CHECKING:
    from mote.contracts.artifact import ArtifactRef
    from mote.contracts.foundation.errors.report import ErrorReport
    from mote.contracts.tool.result import FileChange, ToolMedia

TOOL_INVOCATION_STARTED = "tool_invocation_started"

TOOL_CALL_FINISHED = "tool_call_finished"

TOOLS_CHANGED = "tools_changed"


@dataclass
class ToolsChangedEvent:
    """The executor's bound tool set changed (a tool was de-registered).

    Emitted by the :class:`ToolExecutor` when a tool is removed
    (``deregister_tool``) so downstream views refresh instead of silently
    drifting: the per-turn tool catalog drops the vanished names from its
    incremental frontier (so a later re-registration is re-announced), and the
    compaction pipeline refreshes its reconstructable-tool-name set. Purely an
    observation — the executor's live ``_tools`` map stays the source of truth;
    this only announces *that it changed* and carries the post-change facts a
    consumer needs (which names went away, and the fresh reconstructable set), so
    no consumer needs a back-reference to the executor.
    """

    removed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    generation: int = 0
    reconstructable: List[str] = field(default_factory=list)

    name: ClassVar[str] = TOOLS_CHANGED


@dataclass
class ToolInvocationStartedEvent:
    """Observation emitted at the irreversible tool invocation boundary."""

    identity: ToolInvocationIdentity
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    scope: tuple[object, ...] = ()

    name: ClassVar[str] = TOOL_INVOCATION_STARTED


@dataclass
class ToolCallFinishedEvent:
    """Safe observation of a succeeded, failed, or rejected tool call."""

    identity: ToolInvocationIdentity
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_response: Any = None
    outcome: Literal["succeeded", "failed", "rejected"] = "succeeded"
    #: Structured failure record on a non-success result (``ErrorReport``), mirrored
    #: from the ``ToolResult``; ``None`` on success or for a legacy output-only fail.
    error: Optional["ErrorReport"] = None
    #: Structured media the tool produced (``list[ToolMedia]``: image/pdf artifacts),
    #: mirrored from the ``ToolResult`` so the view layer folds a media block from the
    #: fact instead of sniffing ``tool_response`` text / reverse-engineering a path.
    media: list["ToolMedia"] = field(default_factory=list)
    #: Durable non-media products emitted by the tool. References are opaque;
    #: consumers must not reinterpret them as filesystem paths or media payloads.
    artifacts: list["ArtifactRef"] = field(default_factory=list)
    #: Structured file modifications the tool made (``list[FileChange]``: path/old/new),
    #: mirrored from the ``ToolResult`` so the view layer renders the change from the
    #: fact — side-by-side on a rich host, a synthesized coloured diff on a text host —
    #: instead of sniffing ``tool_response`` text for a diff shape.
    file_changes: list["FileChange"] = field(default_factory=list)
    #: Execution lineage (``ScopePath``) this call ran under. ``()`` = top level.
    scope: tuple[object, ...] = ()

    name: ClassVar[str] = TOOL_CALL_FINISHED
