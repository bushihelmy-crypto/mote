"""CommandChannel ABC and shared media helpers."""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from metagpt.common.const import IMAGES, PDFS, TOOL_CALL_ID, TOOL_CALLS
from metagpt.common.prompt.refs import lower as _lower_symbols
from metagpt.common.prompt.refs import normalize_vocabulary
from metagpt.common.schema import CauseBy, UserMessage

if TYPE_CHECKING:
    from metagpt.common.base.think_engine import BaseThinkEngine
    from metagpt.common.interface import MessageStore


class CommandChannel(ABC):
    """Protocol-specific prompt/call/parse strategy for the react loop."""

    def vocabulary(self) -> dict:
        """Map each prompt symbol (``Sym``/value) to this protocol's surface text.

        Shared prompt prose names protocol mechanics only via ``⟦...⟧`` symbols
        (see ``common.prompt.refs``); this vocabulary is how THIS channel renders
        them. ``lower()`` substitutes through it at the end of prompt assembly,
        so e.g. ``CTL_FINISH`` becomes "emit <end></end>" under XML and "stop
        calling tools and reply with plain text" under native — the native render
        therefore never contains ``<end></end>``.

        Default is empty: a channel with no protocol mechanics in prose needs no
        vocabulary. The invariant test asserts every symbol used in registered
        prose has a surface in every channel's vocabulary.
        """
        return {}

    def lower(self, text: str) -> str:
        """Substitute every ``⟦symbol⟧`` in ``text`` with this protocol's surface.

        Raises ``UnknownSymbolError`` on any symbol missing from ``vocabulary()``
        — a build-time failure so an unregistered/typo'd symbol never leaks to
        the model verbatim. Returns ``text`` unchanged when it holds no symbols.
        """
        return _lower_symbols(text, normalize_vocabulary(self.vocabulary()))

    @abstractmethod
    def output_format(self) -> str:
        """System-prompt OUTPUT section text for this protocol ("" if none)."""

    def command_guide(self) -> str:
        """System-prompt "# Using commands" section text for this protocol.

        Protocol-specific command-usage instructions (the ${command_guide}
        section). XML supplies the <end></end> / command-tag mechanics; native
        supplies tool-call mechanics. Default "" => no section, so the static
        prompt never hard-codes one protocol's mechanics for the other.
        """
        return ""

    def command_hint(self) -> str:
        """Per-turn user-prompt command hint for this protocol ("" if none).

        Supplied as CMD_PROMPT's ${command_hint} section (the user prompt sent
        each turn). XML carries the "ONE and ONLY ONE command block ... <end></end>"
        instruction; native supplies "" so the model is never told to emit
        <end></end> (which it would otherwise echo as literal text). Mirrors
        command_guide(), but for the per-turn user prompt rather than the system
        prompt.
        """
        return ""

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
    async def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
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

    async def is_terminal(self, think_engine: "BaseThinkEngine") -> bool:
        """Whether the react loop should stop after this think round.

        Async because a channel may need to await the think task to finish
        before it can read its result (see NativeToolChannel) -- the loop checks
        this right after launching the think, so the result must be joined first
        to avoid reading the *previous* round's output.

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
