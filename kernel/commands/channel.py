"""Command-channel contract and shared media helpers."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Optional

from mote.contracts.conversation import CauseBy, Message, UserMessage, encode_message
from mote.contracts.conversation.fields import IMAGES, PDFS
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import ModelTurn, TextAction
from mote.contracts.output import OutputRepresentationCapabilities
from mote.kernel.commands.contracts import ExecutedCommand, HistoryProjection
from mote.kernel.commands.symbols import lower as _lower_symbols
from mote.kernel.commands.symbols import normalize_vocabulary
from mote.kernel.output.binding import negotiate_output_binding

MediaMaterializer = Callable[[list[ExecutedCommand]], Awaitable[tuple[list[str], list[str]]]]


async def _no_media(
    _executed: list[ExecutedCommand],
) -> tuple[list[str], list[str]]:
    return [], []


#: The protocol-specific ``${placeholder}`` names the prompt templates expect
#: every channel's ``prompt_vars()`` to fill (the system-prompt command_guide
#: section). The builder asserts a channel covers these, so a partial dict fails
#: the build instead of leaking a literal ``${command_guide}`` to the model.
#: Extending the prompt with a new protocol section is a three-line change: add
#: its key here, a ``${key}`` in the template, and the value in each channel's
#: ``prompt_vars()`` — nothing in InferenceInputs / InferenceContext / collect_context
#: changes.
PROMPT_VAR_KEYS = ("command_guide", "tool_usage_guide")


class CommandChannel(ABC):
    """Protocol-specific prompt/call/parse strategy for the react loop."""

    def __init__(self, media_materializer: MediaMaterializer | None = None) -> None:
        self._media_materializer = media_materializer or _no_media

    def vocabulary(self) -> dict:
        """Map each prompt symbol (``Sym``/value) to this protocol's surface text.

        Shared prompt prose names protocol mechanics only via ``⟦...⟧`` symbols
        (see ``kernel.prompt.refs``); this vocabulary is how THIS channel renders
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
        the template substitutions, so the channel — not InferenceInputs/InferenceContext
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
        """Whether static built-in/pipeline definitions need a prose catalog.

        True for protocols with no API-native tool channel (XML), which need
        built-ins in the system prompt and dynamic pipelines in reminders. False
        for native tool-use: the provider receives callable specs through ``tools=``.
        Hot-reloadable MCP definitions are a deliberate exception handled by the
        per-turn reminder source for both protocols; this flag only controls the
        static built-in/pipeline prose catalog.

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

    def for_model(self, endpoint, *, output_schema=None):
        """Return a canonical channel profiled by endpoint capabilities."""
        return self

    @abstractmethod
    def tool_specs(self, catalog, output_contract=None) -> Optional[list[dict]]:
        """Native tool specs to pass to the LLM, or None for the text channel."""

    @staticmethod
    def history_projection(messages: Sequence[Message]) -> HistoryProjection:
        payload = [encode_message(message) for message in messages]
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return HistoryProjection(messages=tuple(messages), fingerprint=fingerprint)

    @abstractmethod
    async def project_call(self, command_rsp: str, executed: list[ExecutedCommand]) -> HistoryProjection:
        """Project an assistant call into immutable history data."""

    @abstractmethod
    async def project_results(self, executed: list[ExecutedCommand]) -> HistoryProjection:
        """Project tool results into immutable history data."""

    async def project_turn(self, command_rsp: str, executed: list[ExecutedCommand]) -> HistoryProjection:
        call = await self.project_call(command_rsp, executed)
        results = await self.project_results(executed)
        return self.history_projection([*call.messages, *results.messages])

    def turn_signature(self, result: InferenceResult) -> str:
        """A stable string identifying this turn, for duplicate detection.

        XML uses the raw response text; native uses the structured calls (text
        may be empty or repeat while the actual calls differ). Default returns
        the response text -- overridden by channels that have a better signal.
        """
        return result.content or ""

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

    async def model_turn(self, result: InferenceResult) -> ModelTurn:
        """Normalize the completed response into provider-independent actions."""
        content = result.content or ""
        return ModelTurn(content=content, actions=[TextAction(content=content)] if content else [])

    def project_output_feedback(self, feedback) -> HistoryProjection:
        return self.history_projection(
            [
                UserMessage(
                    content=self.render_output_feedback(feedback),
                    cause_by=CauseBy.RUN_COMMAND,
                )
            ]
        )

    async def project_output_candidate(
        self,
        content: str,
        candidate,
        *,
        accepted: bool,
        feedback=None,
    ) -> HistoryProjection:
        call = await self.project_call(content, [])
        messages = list(call.messages)
        if feedback is not None:
            messages.extend(self.project_output_feedback(feedback).messages)
        return self.history_projection(messages)

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


def join_command_outputs(executed: list[ExecutedCommand]) -> str:
    """Join this round's executed-command outputs, or the no-commands notice.

    The single definition of "what the round's outputs string is": the
    blank-line-joined per-command outputs, or ``NO_VALID_COMMANDS`` when nothing
    ran. Used by both the react loop and ``XmlCommandChannel.record_turn``.
    """
    return "\n\n".join(entry.output for entry in executed) if executed else NO_VALID_COMMANDS


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
