"""ToolResult — structured return type for all tool executions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from metagpt.common.const.tools import ERROR_PREFIX  # noqa: F401 (re-export for backward compat)

# ToolError now lives in the global exception system; re-exported here so the
# hundreds of ``from metagpt.executor.tool_result import ToolError`` /
# ``raise ToolError(...)`` call sites are unchanged and auto-upgraded to typed.
from metagpt.common.exception import ToolError  # noqa: F401 (re-export for backward compat)


@dataclass
class ToolResult:
    """Structured result from a tool execution.

    Attributes:
        output: Text representation for the LLM (always a string). When the
            result carries media, this is the textual summary/placeholder that
            goes into the tool_result message (e.g. "Read image (42KB)").
        success: Whether the tool execution succeeded.
        data: Optional raw structured data for hooks/downstream consumers.
        images: Base64-encoded images to surface to the model as a supplemental
            multimodal message. Each entry is a base64 string (no data: prefix).
        pdfs: Base64-encoded PDF documents, surfaced the same way as images.
    """

    output: str
    success: bool = True
    data: Any = field(default=None, repr=False)
    images: list[str] = field(default_factory=list)
    pdfs: list[str] = field(default_factory=list)

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


