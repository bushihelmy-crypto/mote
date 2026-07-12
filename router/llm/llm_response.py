"""LLMResponse — structured result for the native tool-use channel.

The legacy ``aask()`` path returns a plain ``str`` (the model's text), and the
XML command protocol parses commands out of that text. The native tool-use
channel instead needs to carry *both* the assistant's text and any structured
tool calls the model emitted. ``LLMResponse`` is that carrier.

Kept deliberately small and provider-agnostic: providers normalize their own
wire format (OpenAI ``tool_calls`` / Anthropic ``tool_use`` blocks) into this
shape via ``BaseLLM.get_choice_tool_calls``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMToolCall:
    """One structured tool call emitted by the model.

    Attributes:
        id: Provider-assigned call id (e.g. OpenAI ``tool_calls[i].id``), used to
            pair the eventual tool result back to this call. May be "" if the
            provider does not supply one.
        name: The tool/command name the model chose to invoke.
        arguments: Already-parsed keyword arguments (a dict), not a JSON string.
    """

    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Text + structured tool calls from one native-tool-use completion.

    Attributes:
        content: The assistant's free text (may be "" when the model only calls
            tools).
        tool_calls: Normalized tool calls; empty list when the model returned
            plain text (the XML path never populates this).
    """

    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
