"""Kernel output-representation negotiation."""
from __future__ import annotations

from mote.contracts.output import (
    OutputBinding,
    OutputBindingDecision,
    OutputBindingKind,
    OutputRepresentationCapabilities,
)

FINAL_OUTPUT_TOOL_NAME = "__mote_submit_output"


def negotiate_output_binding(*, is_text: bool, capabilities: OutputRepresentationCapabilities) -> OutputBindingDecision:
    if is_text:
        if not capabilities.supports_text:
            raise ValueError("channel cannot represent text output")
        return OutputBindingDecision(OutputBinding(OutputBindingKind.TEXT), capabilities=capabilities)

    reasons: list[str] = []
    if capabilities.supports_native_schema:
        return OutputBindingDecision(OutputBinding(OutputBindingKind.NATIVE_SCHEMA), capabilities=capabilities)
    reasons.append("native_schema_not_supported")

    if capabilities.supports_semantic_tool:
        return OutputBindingDecision(
            OutputBinding(OutputBindingKind.NATIVE_TOOL, FINAL_OUTPUT_TOOL_NAME),
            tuple(reasons),
            capabilities,
        )
    reasons.append("semantic_tool_not_supported")

    if capabilities.supports_prompted_json:
        return OutputBindingDecision(
            OutputBinding(OutputBindingKind.PROMPTED_JSON),
            tuple(reasons),
            capabilities,
        )
    raise ValueError("channel cannot represent structured output")
