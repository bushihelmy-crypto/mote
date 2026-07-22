"""CommandChannel ABC and shared media helpers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from mote.common.const import IMAGES, PDFS
from mote.common.output_binding import negotiate_output_binding
from mote.common.prompt.refs import lower as _lower_symbols
from mote.common.prompt.refs import normalize_vocabulary
from mote.common.schema import CauseBy, ModelTurn, OutputRepresentationCapabilities, TextAction, UserMessage

if TYPE_CHECKING:
    from mote.common.base.think_engine import BaseThinkEngine
    from mote.common.interface import MessageStore


#: The protocol-specific ``${placeholder}`` names the prompt templates expect
#: every channel's ``prompt_vars()`` to fill (the system-prompt command_guide
#: section). The builder asserts a channel covers these, so a partial dict fails
#: the build instead of leaking a literal ``${command_guide}`` to the model.
#: Extending the prompt with a new protocol section is a three-line change: add
#: its key here, a ``${key}`` in the template, and the value in each channel's
#: ``prompt_vars()`` — nothing in ThinkInputs / ThinkContext / collect_context
#: changes.
PROMPT_VAR_KEYS = ("command_guide", "tool_usage_guide")


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

    def prompt_vars(self) -> dict[str, str]:
        """Named ``${placeholder}`` fills this protocol contributes to the prompts.

        The single seam by which a channel injects its protocol-specific prompt
        sections — command_guide (system "# Using commands" mechanics) and
        tool_usage_guide (the static orientation on how tools are called and how
        the tool categories relate). The builder merges this dict straight into
        the template substitutions, so the channel — not ThinkInputs/ThinkContext
        — owns every protocol section.

        Symmetric with ``vocabulary()``: that supplies inline ``⟦symbol⟧``
        surfaces, this supplies block-level ``${section}`` text. XML fills
        command_guide with its ``<end></end>`` / command-tag mechanics and
        tool_usage_guide with the in-prompt catalog orientation; native fills
        command_guide with tool-call mechanics (never the ``<end></end>`` marker)
        and leaves tool_usage_guide empty (its tools ride the API ``tools=``
        param, so the system prompt needs no catalog orientation).

        Must cover ``PROMPT_VAR_KEYS`` (the placeholders the templates reference);
        the default fills them all with "" — a channel that adds no protocol
        sections is valid.
        """
        return {key: "" for key in PROMPT_VAR_KEYS}

    def wants_tool_catalog(self) -> bool:
        """Whether the tool catalog (built-in / MCP / pipeline schemas) must be
        described in the system prompt.

        True for protocols with no API-native tool channel (XML), which can only
        make the model aware of a tool by describing it in prose. False for
        native tool-use: the provider already receives every tool via the API
        ``tools=`` param (``get_native_tool_specs`` — built-in + MCP), so copying
        the same schemas into the system prompt is pure token waste and bloats
        the cacheable prefix. Native tool definitions are never put in the
        system prompt.

        Default True (safe: describe tools). Native overrides to False.
        """
        return True

    @abstractmethod
    def output_capabilities(self) -> OutputRepresentationCapabilities:
        """Declare the output representations implemented by this channel."""

    def output_binding_decision(self, *, is_text: bool):
        return negotiate_output_binding(is_text=is_text, capabilities=self.output_capabilities())

    def output_binding(self, *, is_text: bool):
        """Return the selected binding; prefer the decision when provenance matters."""
        return self.output_binding_decision(is_text=is_text).binding

    def for_llm(self, llm, *, output_schema=None):
        """Return a wire-equivalent channel profiled for the resolved LLM."""
        return self

    @abstractmethod
    def tool_specs(self, executor, output_contract=None) -> Optional[list[dict]]:
        """Native tool specs to pass to the LLM, or None for the text channel."""

    @abstractmethod
    async def iter_commands(self, think_engine: "BaseThinkEngine", valid_names: set[str]) -> AsyncGenerator[dict, None]:
        """Yield unified-IR commands from a completed ThinkEngine output.

        Each item: ``{command_name, args, id, status, error_msg}``. ``id`` is the
        provider tool-call id for native mode (used to pair tool results), or
        None for XML. Unknown command names (not in valid_names) are filtered
        out. Both channels block on the think task being done before reading.
        """
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator for typing

    @abstractmethod
    async def record_call(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        """Record the assistant message that PRECEDES this turn's tool results.

        The first half of a round, split out so the loop can persist (and flush)
        it *before* executing an EXTERNAL-effect tool — a mid-side-effect crash
        then leaves a healable dangling call on resume rather than losing the
        whole turn. Reads only the pre-execution-stable fields of ``executed``
        (``id`` / ``name`` / ``args``), so it is safe to call before the bodies
        run.

        XML records the assistant's text; native records an assistant message
        carrying the tool_calls (paired later by id with the results).
        """

    @abstractmethod
    async def record_results(self, memory: "MessageStore", executed: list[dict]) -> None:
        """Record this turn's tool RESULTS (the second half of a round).

        Runs after the bodies have filled ``executed`` with ``output`` /
        ``success`` / media. XML merges the outputs into one user message; native
        emits one tool-result message per executed call (paired by id), plus a
        supplemental media message when any result carried images/PDFs.
        """

    async def record_turn(self, memory: "MessageStore", command_rsp: str, executed: list[dict]) -> None:
        """Record one whole think->act round into memory in this protocol's shape.

        The single-shot path (call + results together) for turns that need no
        pre-execution checkpoint. Kept as the shared composition of
        :meth:`record_call` + :meth:`record_results` so the two-phase (checkpoint)
        path and this one-phase path can never drift.

        Args:
            memory: the Role's memory (has .add()).
            command_rsp: the assistant's text for this turn.
            executed: list of ``{id, name, output, success}`` for the commands
                that ran this turn (in order).

        XML records a single assistant text + one merged user message of outputs.
        Native records an assistant message carrying tool_calls + one tool-result
        message per executed call (paired by id), as the API requires.
        """
        await self.record_call(memory, command_rsp, executed)
        await self.record_results(memory, executed)

    def turn_signature(self, think_engine: "BaseThinkEngine") -> str:
        """A stable string identifying this turn, for duplicate detection.

        XML uses the raw response text; native uses the structured calls (text
        may be empty or repeat while the actual calls differ). Default returns
        the response text -- overridden by channels that have a better signal.
        """
        return think_engine.result.content or ""

    def react_result(self, outputs: str) -> str:
        """The react loop's PUBLISHED result message for one completed action round.

        This is the loop's return envelope handed to the environment / other
        roles (via publish_message) — NOT a prompt and NOT this role's own
        history (record_turn writes history separately). Because it is an
        orchestration signal it is protocol-flavored: XML overrides this to ask
        an orchestrator to mark the task finished (the <end></end>-era contract);
        native keeps the plain outputs, since a native turn finishes via a plain
        text reply (see is_terminal / the loop's _finish) and never needs the XML
        orchestration phrasing. Default: the joined outputs verbatim.
        """
        return outputs

    async def model_turn(self, think_engine: "BaseThinkEngine") -> ModelTurn:
        """Normalize the completed response into provider-independent actions."""
        if not think_engine.done:
            await think_engine.join()
        content = think_engine.result.content or ""
        return ModelTurn(content=content, actions=[TextAction(content=content)] if content else [])

    async def record_output_feedback(self, memory: "MessageStore", feedback) -> None:
        """Project provider-independent correction feedback into conversation history."""
        await memory.add(
            UserMessage(
                content=self.render_output_feedback(feedback),
                cause_by=CauseBy.RUN_COMMAND,
            )
        )

    async def record_output_candidate(
        self,
        memory: "MessageStore",
        content: str,
        candidate,
        *,
        accepted: bool,
        feedback=None,
    ) -> None:
        """Record one semantic final submission in this channel's wire shape."""
        await self.record_turn(memory, content, [])
        if feedback is not None:
            await self.record_output_feedback(memory, feedback)

    @staticmethod
    def render_output_feedback(feedback) -> str:
        lines = [feedback.summary]
        for issue in feedback.issues:
            path = ".".join(str(part) for part in issue.path) or "<root>"
            lines.append(f"- {path} [{issue.code}]: {issue.message}")
        return "\n".join(lines)


#: Notice surfaced as the round's "outputs" when no valid command ran this turn.
#: Single source shared by the loop (building the react result) and the XML
#: channel (building its user-message outputs), so the two never drift.
NO_VALID_COMMANDS = "No valid commands found for execution, pay attention to the output format."


def join_command_outputs(executed: list[dict]) -> str:
    """Join this round's executed-command outputs, or the no-commands notice.

    The single definition of "what the round's outputs string is": the
    blank-line-joined per-command outputs, or ``NO_VALID_COMMANDS`` when nothing
    ran. Used by both the react loop and ``XmlCommandChannel.record_turn``.
    """
    return "\n\n".join(e["output"] for e in executed) if executed else NO_VALID_COMMANDS


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
