"""CommandChannel ABC and shared media helpers."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from metagpt.common.const import IMAGES, PDFS, TOOL_CALL_ID, TOOL_CALLS
from metagpt.common.schema import CauseBy, UserMessage

if TYPE_CHECKING:
    from metagpt.common.base.think_engine import BaseThinkEngine
    from metagpt.common.interface import MessageStore


class CommandChannel(ABC):
    """Protocol-specific prompt/call/parse strategy for the react loop."""

    @abstractmethod
    def output_format(self) -> str:
        """System-prompt OUTPUT section text for this protocol ("" if none)."""

    @abstractmethod
    def tool_specs(self, executor) -> Optional[list[dict]]:
        """Native tool specs to pass to the LLM, or None for the text channel."""

    @abstractmethod
    async def iter_commands(
        self, think_engine: "BaseThinkEngine", valid_names: set[str]
    ) -> AsyncGenerator[dict, None]:
        """Yield unified-IR commands from a completed ThinkEngine output.

        Each item: ``{command_name, args, id, status, error_msg}``. ``id`` is the
        provider tool-call id for native mode (used to pair tool results), or
        None for XML. Unknown command names (not in valid_names) are filtered
        out. Both channels block on the think task being done before reading.
        """
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator for typing

    @abstractmethod
    def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        """Record one think->act round into memory in this protocol's shape.

        Args:
            memory: the Role's memory (has .add()).
            command_rsp: the assistant's text for this turn.
            executed: list of ``{id, name, output, success}`` for the commands
                that ran this turn (in order).

        XML records a single assistant text + one merged user message of outputs.
        Native records an assistant message carrying tool_calls + one tool-result
        message per executed call (paired by id), as the API requires.
        """

    def turn_signature(self, think_engine: "BaseThinkEngine") -> str:
        """A stable string identifying this turn, for duplicate detection.

        XML uses the raw response text; native uses the structured calls (text
        may be empty or repeat while the actual calls differ). Default returns
        the response text -- overridden by channels that have a better signal.
        """
        return think_engine.result.content or ""

    def is_terminal(self, think_engine: "BaseThinkEngine") -> bool:
        """Whether the react loop should stop after this think round.

        Each protocol signals "done" differently:
          - XML: the model emits an ``End`` command, which deactivates the Role;
            the loop already stops when the next ``_think`` returns False, so the
            channel itself never reports a terminal turn (default False).
          - native: the model finishes by replying with plain text and no
            tool_calls -- see NativeToolChannel.
        """
        return False


def _collect_media(executed: list[dict]) -> tuple[list[str], list[str]]:
    """Gather base64 images / PDFs from this turn's executed commands.

    Returns (images, pdfs). Tools that read media (e.g. Read on an image or
    PDF) put a textual placeholder in their tool_result output and the actual
    base64 bytes here, so the model receives them as a separate multimodal
    message rather than stuffed into a tool_result string.
    """
    images: list[str] = []
    pdfs: list[str] = []
    for e in executed:
        images.extend(e.get("images") or [])
        pdfs.extend(e.get("pdfs") or [])
    return images, pdfs


def _media_message(images: list[str], pdfs: list[str]):
    """Build the supplemental user message carrying media, or None if empty.

    Media rides in metadata[IMAGES]/[PDFS]; the LLM client's format_msg renders
    those into multimodal content blocks. A short text body anchors the message
    so providers that require non-empty content stay happy.
    """
    if not images and not pdfs:
        return None
    msg = UserMessage(
        content="Attached media from the tool result(s) above.",
        cause_by=CauseBy.RUN_COMMAND,
    )
    if images:
        msg.metadata[IMAGES] = images
    if pdfs:
        msg.metadata[PDFS] = pdfs
    return msg
