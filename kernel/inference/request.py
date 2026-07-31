"""Inference request inputs prepared by the context provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mote.contracts.output import OutputBindingDecision


@dataclass
class InferenceRequest:
    """The complete argument set for InferenceEngine.start() — ContextProvider's product.

    Bundles the values the think step feeds into the engine so the flow faces
    a single entry point (context_provider.prepare()) instead of the
    collect → build → prepare_llm_request → tool_specs glue chain.
    """

    req: list
    system_prompt: str
    tool_specs: Any
    output_binding: OutputBindingDecision
    command_channel: Any
    output_schema: dict
    schema_fingerprint: str
    tool_snapshot: Any = None
    tool_projection_fingerprint: str = ""
    protocol_fingerprint: str = ""
    vocabulary_fingerprint: str = ""
    prompt_section_set_fingerprint: str = ""
