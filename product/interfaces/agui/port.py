#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``AguiPort`` — the AG-UI input half (Phase 2 upstream + Phase 3 HITL loop).

AG-UI is a **stateless-request** protocol: a ``POST /agent/{id}/run`` carries the
turn's user message in its body, and the SSE response streams that turn's events.
So the port's turn boundary is trivial — :meth:`read_turn` returns the one
message the request injected, then ``None`` (the turn is over; the server closes
the SSE stream). There is no long-lived stdin to poll.

The interactive round-trips (``ask`` / ``ask_questions`` / ``decide_approval``)
need a **back-channel** the frontend answers on. Because ``/run`` and the reply
live in *different* HTTP requests, the port can't block on itself — it:

1. mints a correlation id from the shared :class:`PromptBroker`,
2. emits the prompt frame down THIS run's SSE ``sink`` (built by the pure wire
   mapper, so the shape stays transport-free), and
3. awaits the broker future the separate ``POST /respond`` handler resolves.

When no ``sink``/``broker`` is wired (a read-only Phase-2 stream), the round-trips
fall back to **safe non-blocking defaults** (deny approvals, empty answers) so a
turn never hangs on a human who has nowhere to reply. A bounded timeout gives the
same fallback if a wired frontend never answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Awaitable, Callable, Optional

from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.interaction import (
    ApprovalRequest,
    AskUserQuestionAnswer,
    AskUserQuestionAnswers,
    AskUserQuestionInput,
)
from mote.contracts.interaction.handoff import DriverHandoffHandle, HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.surface import LiveSurfaceSession
from mote.product.interaction.ports import DriverControlBinding
from mote.product.interfaces.agui import wire as agui
from mote.product.presentation.events.events import ApprovalDecision
from mote.product.presentation.projection.approval import approval_action, approval_preview, approval_risk
from mote.product.presentation.wire_types import WireMapping, to_wire_json
from mote.product.session_hosting.prompt_broker import PromptBroker, PromptHandle, PromptKind, PromptScope
from mote.runtime.telemetry.logging import logger

#: Async wire sink (same contract as ``AguiConsumer.Sink``): one dict → the wire.
WireObject = WireMapping
Sink = Callable[[WireObject], Awaitable[None]]

#: Seconds to wait for a human reply before falling back to the safe default.
#: Bounds a turn so a frontend that opened a stream then vanished can't wedge the
#: resident session forever; generous because a human is in the loop.
DEFAULT_PROMPT_TIMEOUT_S = 600.0


class AguiPort:
    """One turn's inbound edge for an AG-UI ``/run`` request.

    Constructed per request with the turn's ``text``; the server also injects the
    SSE ``sink`` + the app-scoped ``broker`` + this run's ``thread_id``/``run_id``
    so interactive prompts can stream down the live SSE and block on a
    back-channel reply. Implements the
    :class:`~mote.product.interaction.ports.InputPort` protocol structurally
    (duck-typed — no nominal base, matching the leaf-interface contract).
    """

    def __init__(
        self,
        text: str = "",
        *,
        sink: Optional[Sink] = None,
        broker: Optional["PromptBroker"] = None,
        thread_id: str = "",
        run_id: str = "",
        principal: str = "",
        agent_id: str = "",
        timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    ) -> None:
        self._text: Optional[str] = text
        self._sink = sink
        self._broker = broker
        self._thread_id = thread_id
        self._run_id = run_id
        self._principal = principal
        self._agent_id = agent_id
        self._timeout_s = timeout_s
        self._closed = False

    def bind_driver_control(self, binding: DriverControlBinding) -> None:
        return None

    async def start(self) -> None:
        return None

    def take_turn_images(self) -> list[Mapping[str, JsonValue]]:
        return []

    def request_exit(self) -> None:
        self._closed = True

    async def open_handoff(
        self,
        request: HandoffRequest,
        handle: DriverHandoffHandle,
        surface: LiveSurfaceSession | None = None,
    ) -> HumanHandoffOutcome:
        return HumanHandoffOutcome(status=HandoffStatus.UNAVAILABLE)

    def stage_restore(self, text: str) -> None:
        return None

    # ------------------------------------------------------------------
    # Turn boundary
    # ------------------------------------------------------------------
    async def read_turn(self) -> Optional[str]:
        """Return the request's injected message once, then ``None`` (turn done).

        AG-UI drives exactly one turn per request, so the first read yields the
        body text and the next signals end-of-input — the driver's ``read_turn``
        loop runs a single turn and unwinds.
        """
        text, self._text = self._text, None
        return text

    # ------------------------------------------------------------------
    # HITL round-trips — emit a prompt frame, await the back-channel reply
    # ------------------------------------------------------------------
    def _can_prompt(self) -> bool:
        """True when both a live SSE sink and the shared broker are wired."""
        return self._sink is not None and self._broker is not None and not self._closed

    async def _emit(self, frame: WireObject) -> None:
        if self._sink is None:
            return
        try:
            await self._sink(frame)
        except Exception as exc:  # noqa: BLE001 — a dead socket must not crash the turn
            logger.warning(f"AguiPort: prompt frame write failed: {exc}")

    def _open_prompt(self, kind: PromptKind) -> tuple[PromptHandle, asyncio.Future[JsonValue]]:
        assert self._broker is not None
        return self._broker.open(
            PromptScope(
                principal=self._principal,
                agent_id=self._agent_id,
                thread_id=self._thread_id,
                run_id=self._run_id,
                kind=kind,
            ),
            ttl_seconds=self._timeout_s,
        )

    async def _await_reply(self, handle: PromptHandle, future: asyncio.Future[JsonValue]) -> JsonValue:
        """Await the broker future for *prompt_id*, bounded by the timeout.

        Returns the posted payload, or ``None`` on timeout / cancellation
        (shutdown) / no broker — the caller maps ``None`` to its safe default.
        """
        if self._broker is None:
            return None
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        except asyncio.TimeoutError:
            logger.info(f"AguiPort: prompt {handle.prompt_id} timed out; using safe default")
            self._broker.discard(handle.prompt_id)
            return None
        except asyncio.CancelledError:
            # cancel_all() on shutdown / teardown — fall back, don't propagate a
            # cancellation into the turn (the run is unwinding anyway).
            self._broker.discard(handle.prompt_id)
            return None

    async def ask(self, ctx: object, question: str) -> str:
        """Free-text question: stream a ``question`` frame, await the reply text.

        ``options``/``multi`` are accepted only for the ``PortHumanChannel``
        degrade path (structured questions normally arrive via
        :meth:`ask_questions`). Without a back-channel we return empty so the
        turn never blocks on an answer that can't arrive.
        """
        if not self._can_prompt():
            logger.debug("AguiPort.ask: no HITL back-channel; returning empty answer")
            return ""
        handle, future = self._open_prompt(PromptKind.QUESTION)
        await self._emit(
            _wire_object(agui.question_prompt(question_id=handle.prompt_id, question=question, binding=handle))
        )
        payload = await self._await_reply(handle, future)
        return self._answer_text(payload)

    async def ask_questions(self, ctx: object, questions: AskUserQuestionInput) -> AskUserQuestionAnswers:
        """Structured multiple-choice: stream one ``question`` frame, await answers.

        Emits the full question payload so a rich frontend can render selects; the
        ``/respond`` body carries ``{answers:[{header,question,selected,free_text}]}``
        which we parse straight back into :class:`AskUserQuestionAnswers`. No
        back-channel → empty answers (non-blocking).
        """
        if not self._can_prompt():
            return AskUserQuestionAnswers(answers=[])
        handle, future = self._open_prompt(PromptKind.QUESTION)
        await self._emit(
            _wire_object(
                agui.question_prompt(
                    question_id=handle.prompt_id,
                    question=self._first_question_text(questions),
                    structured=to_wire_json(self._serialize_questions(questions)),
                    binding=handle,
                )
            )
        )
        payload = await self._await_reply(handle, future)
        return self._parse_answers(payload, questions)

    async def decide_approval(self, ctx: object, request: ApprovalRequest) -> ApprovalDecision:
        """Gated action: stream an ``approval`` frame, await accept/reject/edit.

        The engine hands a language-neutral ``ApprovalRequest``; we localize the
        action/preview/risk here (same seam the terminal + Textual ports use),
        stream the ``approval`` frame, and await ``{outcome, editedArgs?}`` from
        ``/respond``. No back-channel (or a timeout) → reject: the fail-safe
        answer to "may I run this?" without a human is no.
        """
        if not self._can_prompt():
            logger.debug("AguiPort.decide_approval: no HITL back-channel; rejecting (fail-safe)")
            return ApprovalDecision(approval_id="", outcome="reject")
        handle, future = self._open_prompt(PromptKind.APPROVAL)
        await self._emit(
            _wire_object(
                agui.approval_prompt(
                    approval_id=handle.prompt_id,
                    tool_name=request.tool_name,
                    action=approval_action(request),
                    args_preview=approval_preview(request),
                    risk=approval_risk(request),
                    binding=handle,
                )
            )
        )
        payload = await self._await_reply(handle, future)
        return self._parse_decision(payload, handle.prompt_id)

    # ------------------------------------------------------------------
    # Payload parsing (opaque reply dict → typed result); tolerant of shape
    # ------------------------------------------------------------------
    @staticmethod
    def _answer_text(payload: JsonValue) -> str:
        """A free-text ``ask`` reply — accept ``{answer}`` or a bare string."""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, Mapping):
            val = payload.get("answer")
            if isinstance(val, str):
                return val
        return ""

    @staticmethod
    def _first_question_text(questions: AskUserQuestionInput) -> str:
        return questions.questions[0].question

    @staticmethod
    def _serialize_questions(questions: AskUserQuestionInput) -> JsonValue:
        return freeze_json(questions.model_dump(mode="json"), path="agui.questions")

    def _parse_answers(self, payload: JsonValue, questions: AskUserQuestionInput) -> AskUserQuestionAnswers:
        """Map a ``{answers:[...]}`` reply into structured answers, else empty."""
        if not isinstance(payload, Mapping):
            return AskUserQuestionAnswers(answers=[])
        raw = payload.get("answers")
        out: list[AskUserQuestionAnswer] = []
        if isinstance(raw, tuple):
            for a in raw:
                if not isinstance(a, Mapping):
                    continue
                sel = a.get("selected")
                selected = [item for item in sel if isinstance(item, str)] if isinstance(sel, tuple) else []
                header = a.get("header")
                question = a.get("question")
                free_text = a.get("free_text") or a.get("freeText")
                out.append(
                    AskUserQuestionAnswer(
                        header=header if isinstance(header, str) else "",
                        question=question if isinstance(question, str) else "",
                        selected=selected,
                        free_text=free_text if isinstance(free_text, str) else "",
                    )
                )
        return AskUserQuestionAnswers(answers=out)

    def _parse_decision(self, payload: JsonValue, prompt_id: str) -> ApprovalDecision:
        """Map a ``{outcome, editedArgs?}`` reply to an ``ApprovalDecision``.

        A missing/garbled/absent reply rejects (fail-safe). ``outcome`` is
        validated against the four known values; anything else → reject.
        """
        if not isinstance(payload, Mapping):
            return ApprovalDecision(approval_id=prompt_id, outcome="reject")
        outcome = payload.get("outcome")
        if outcome not in ("accept", "reject", "always_allow", "always_deny"):
            outcome = "reject"
        edited = payload.get("editedArgs")
        if edited is not None and not isinstance(edited, Mapping):
            edited = None
        thawed = thaw_json(edited) if edited is not None else None
        if thawed is not None and not isinstance(thawed, dict):
            raise TypeError("edited AG-UI approval arguments must decode to an object")
        return ApprovalDecision(approval_id=prompt_id, outcome=outcome, edited_args=thawed)

    # ------------------------------------------------------------------
    # Control affordances (server owns the run task; these stay inert)
    # ------------------------------------------------------------------
    def signal_interrupt(self, ctx: object = None) -> None:
        """Cancel the in-flight turn. Phase 2/3: no-op (server owns the run task)."""
        return None

    def submit_steer(self, ctx: object, text: str) -> None:
        """Turn-level steering. No-op (no mid-stream steer control yet)."""
        return None

    async def aclose(self) -> None:
        """Mark closed so a late prompt short-circuits to its safe default.

        The broker's pending futures are the *server*'s responsibility to
        ``cancel_all`` on shutdown — a per-request port doesn't own the shared
        broker, only its own emit gate.
        """
        self._closed = True
        if self._broker is not None:
            for kind in PromptKind:
                self._broker.cancel_scope(
                    PromptScope(
                        principal=self._principal,
                        agent_id=self._agent_id,
                        thread_id=self._thread_id,
                        run_id=self._run_id,
                        kind=kind,
                    )
                )


def _wire_object(value: object) -> WireObject:
    wire_value = to_wire_json(value)
    if not isinstance(wire_value, dict):
        raise TypeError("AG-UI frame must be a JSON object")
    return wire_value


__all__ = ["AguiPort", "Sink", "DEFAULT_PROMPT_TIMEOUT_S"]
