"""ToolResult — structured return type for all tool executions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mote.contracts.artifact import ArtifactRef
from mote.contracts.tool.result import FileChange, ToolMedia
from mote.runtime.errors import ErrorReport


@dataclass
class ToolResult:
    """Structured result from a tool execution.

    Attributes:
        output: Text representation for the LLM (always a string). When the
            result carries media, this is the textual summary/placeholder that
            goes into the tool_result message (e.g. "Read image (42KB)").
        success: Whether the tool execution succeeded.
        data: Optional raw structured data for hooks/downstream consumers.
        media: Structured media artifacts this tool produced, each a
            ``ToolMedia(artifact, kind, ref, mime)``. The single
            authoritative field for media — the producer stamps local ``ref``
            and durable ``artifact`` at the source, eliminating any need to
            sniff a path from ``data``. Bytes are resolved only at the model or
            presentation boundary.
        artifacts: Durable non-media products this tool produced. Each entry is
            an opaque typed ``ArtifactRef``; presentation layers resolve or
            degrade it without treating it as a path or base64 media payload.
        file_changes: Structured file modifications this tool made, each a
            ``FileChange(path, old, new)``. The view layer renders these
            directly (side-by-side on a rich host, coloured diff on a text
            host) instead of sniffing the output text. Empty for tools that
            don't modify files.
        error: Structured failure record on a non-success result. Set by the
            executor from the raised exception (``ErrorReport.from_exception``);
            ``output`` is the rendered ``<error>`` block of this same report.
            ``None`` on success or for plain ``ToolResult(success=False)``
            results that only carry an ``output`` string.
        terminate: Whether this result should end the react loop, not just fail
            the call. Set by the executor when a PreToolUse control outcome
            carries ``stop`` (a genuine user rejection at the approval prompt, or
            a hook veto) — distinct from a plain ``deny`` (a recoverable block the
            model can replan around). The loop reads it and clears the active
            signal, ending via the same kill switch the End tool uses.
        retention: Optional lifecycle hint for *this* result, set by the tool
            (the model-facing counterpart to the static ``reconstructable``
            ClassVar). One of the ``RETENTION_*`` values in
            ``common.const.message`` (e.g. "erasable" / "pin"), or ``None`` for
            default. The executor carries it verbatim onto the tool_result
            message's metadata; the compaction layer is the sole interpreter.
            This is pure plumbing here — the field only exists so a tool can
            express intent; how a tool populates it is the tool's concern.
        resource_path: Absolute path of the durable resource this result was
            derived from, set by *reconstructable* read-only tools (Read stamps
            the file it read). The executor/channel carry it verbatim onto the
            tool_result message's metadata (``TOOL_RESULT_RESOURCE_PATH``), where
            :class:`~mote.runtime.context.history.visibility.ContextVisibility` uses it to answer
            "is this file's last read still present in context?". ``None`` for
            results not tied to a re-readable resource. Pure plumbing here, like
            ``retention``.
    """

    output: str
    success: bool = True
    data: Any = field(default=None, repr=False)
    media: list[ToolMedia] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    file_changes: list[FileChange] = field(default_factory=list)
    error: Optional[ErrorReport] = None
    terminate: bool = False
    retention: Optional[str] = None
    resource_path: Optional[str] = None

    @classmethod
    def from_tool_return(cls, raw: Any) -> "ToolResult":
        """Normalize a tool's raw (non-exception) return value into a ToolResult.

        A tool's call() may return either a ToolResult (already structured) or a
        plain value. A plain value is ALWAYS treated as success — failure is
        signalled structurally (raise ToolError, caught by the executor, or
        return ToolResult(success=False)), never by inspecting the output text.
        This lets a successful output begin with any string, including "Error:".
        """
        if isinstance(raw, cls):
            return raw
        output = str(raw) if raw is not None else ""
        return cls(output=output, success=True)
