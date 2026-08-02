"""Provider-neutral model response contracts.

The text-protocol ``aask()`` path returns a plain ``str``, and the
XML command protocol parses commands out of that text. The native tool-use
channel instead needs to carry *both* the assistant's text and any structured
tool calls the model emitted. ``LLMResponse`` is that carrier.

Kept deliberately small and provider-agnostic: integrations normalize their own
wire format (OpenAI ``tool_calls`` / Anthropic ``tool_use`` blocks) into this
shape via ``BaseLLM.get_choice_tool_calls``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mote.contracts.model.invocation import CanonicalToolCall


@dataclass
class WebSearchHit:
    """One search result from a provider-native server-side web search.

    Attributes:
        title: The result page's title.
        url: The result page's URL.
        snippet: An optional short description/excerpt (providers that return one).
    """

    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Text + structured tool calls from one native-tool-use completion.

    Attributes:
        content: The assistant's free text (may be "" when the model only calls
            tools).
        tool_calls: Normalized tool calls; empty list when the model returned
            plain text (the XML path never populates this).
    """

    content: str = ""
    tool_calls: tuple[CanonicalToolCall, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise TypeError("LLM response content must be a string")
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, CanonicalToolCall) for call in calls):
            raise TypeError("LLM response tool calls must be canonical")
        object.__setattr__(self, "tool_calls", calls)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


__all__ = ["LLMResponse", "WebSearchHit"]
