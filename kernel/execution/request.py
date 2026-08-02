"""Complete turn input prepared by the execution context provider."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from mote.contracts.conversation import Message
from mote.contracts.model.invocation import CanonicalToolDefinition
from mote.contracts.output import OutputBindingDecision
from mote.contracts.tool.catalog import ToolBindingSnapshot
from mote.kernel.commands.channel import CommandChannel


@dataclass
class InferenceRequest:
    """Execution-owned inputs handed to the inference operation for one turn."""

    req: list[Message]
    system_prompt: str
    tool_specs: tuple[CanonicalToolDefinition, ...] | None
    output_binding: OutputBindingDecision
    command_channel: CommandChannel
    output_schema: dict[str, JsonValue]
    schema_fingerprint: str
    tool_snapshot: ToolBindingSnapshot | None = None
    tool_projection_fingerprint: str = ""
    protocol_fingerprint: str = ""
    vocabulary_fingerprint: str = ""
    prompt_section_set_fingerprint: str = ""


__all__ = ["InferenceRequest"]
