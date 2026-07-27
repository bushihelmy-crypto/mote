"""Provider-neutral routing-intent extraction owned by the single-agent Kernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from mote.contracts.models.routing import RoutingMessage, RoutingSignals
from mote.contracts.models.tokenization import count_message_tokens, count_string_tokens


def build_routing_signals(
    messages: Sequence[Mapping[str, object]],
    *,
    prompt_text: str = "",
) -> RoutingSignals:
    """Normalize model context into the stable semantic-routing signal contract."""

    normalized = tuple(
        RoutingMessage(
            role=str(message.get("role", "user")),
            content=str(message.get("content", "")),
        )
        for message in messages
    )
    text = prompt_text or "\n".join(message.content for message in normalized if message.content)
    wire = [message.model_dump(mode="python") for message in normalized]
    try:
        estimated_tokens = count_message_tokens(wire, "gpt-3.5-turbo-0125")
    except Exception:
        try:
            estimated_tokens = count_string_tokens(text, "gpt-3.5-turbo-0125")
        except Exception:
            estimated_tokens = 0
    return RoutingSignals(
        messages=normalized,
        prompt_text=text,
        estimated_tokens=estimated_tokens,
        conversation_turns=sum(1 for message in normalized if message.role == "user"),
    )


__all__ = ["build_routing_signals"]
