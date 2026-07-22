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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from mote.cli.consumers._wire import agui
from mote.cli.contracts.view.events import ApprovalDecision
from mote.cli.view.approval import approval_action, approval_preview, approval_risk
from mote.common.logs import logger
from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers, AskUserQuestionInput

if TYPE_CHECKING:
    from mote.cli.serving.prompt_broker import PromptBroker

#: Async wire sink (same contract as ``AguiConsumer.Sink``): one dict → the wire.
Sink = Callable[[Dict[str, Any]], Awaitable[None]]

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
    :class:`~mote.cli.contracts.interface.ports.InputPort` protocol structurally
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
        timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    ) -> None:
        self._text: Optional[str] = text
        self._sink = sink
        self._broker = broker
        self._thread_id = thread_id
        self._run_id = run_id
        self._timeout_s = timeout_s
        self._closed = False

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

    async def _emit(self, frame: Dict[str, Any]) -> None:
        if self._sink is None:
            return
        try:
            await self._sink(frame)
        except Exception as exc:  # noqa: BLE001 — a dead socket must not crash the turn
            logger.warning(f"AguiPort: prompt frame write failed: {exc}")

    async def _await_reply(self, prompt_id: str) -> Optional[Any]:
        """Await the broker future for *prompt_id*, bounded by the timeout.

        Returns the posted payload, or ``None`` on timeout / cancellation
        (shutdown) / no broker — the caller maps ``None`` to its safe default.
        """
        if self._broker is None:
            return None
        fut = self._broker.open(prompt_id)
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout_s)
        except asyncio.TimeoutError:
            logger.info(f"AguiPort: prompt {prompt_id} timed out; using safe default")
            self._broker.discard(prompt_id)
            return None
        except asyncio.CancelledError:
            # cancel_all() on shutdown / teardown — fall back, don't propagate a
            # cancellation into the turn (the run is unwinding anyway).
            self._broker.discard(prompt_id)
            return None

    async def ask(self, ctx: Any, question: str, options: Optional[List[str]] = None, multi: bool = False) -> str:
        """Free-text question: stream a ``question`` frame, await the reply text.

        ``options``/``multi`` are accepted only for the ``PortHumanChannel``
        degrade path (structured questions normally arrive via
        :meth:`ask_questions`). Without a back-channel we return empty so the
        turn never blocks on an answer that can't arrive.
        """
        if not self._can_prompt():
            logger.debug("AguiPort.ask: no HITL back-channel; returning empty answer")
            return ""
        prompt_id = self._broker.new_id("q")  # type: ignore[union-attr]  — guarded by _can_prompt
        await self._emit(agui.question_prompt(question_id=prompt_id, question=question, options=options))
        payload = await self._await_reply(prompt_id)
        return self._answer_text(payload)

    async def ask_questions(self, ctx: Any, questions: "AskUserQuestionInput") -> "AskUserQuestionAnswers":
        """Structured multiple-choice: stream one ``question`` frame, await answers.

        Emits the full question payload so a rich frontend can render selects; the
        ``/respond`` body carries ``{answers:[{header,question,selected,free_text}]}``
        which we parse straight back into :class:`AskUserQuestionAnswers`. No
        back-channel → empty answers (non-blocking).
        """
        if not self._can_prompt():
            return AskUserQuestionAnswers(answers=[])
        prompt_id = self._broker.new_id("q")  # type: ignore[union-attr]
        await self._emit(
            agui.question_prompt(
                question_id=prompt_id,
                question=self._first_question_text(questions),
                structured=self._serialize_questions(questions),
            )
        )
        payload = await self._await_reply(prompt_id)
        return self._parse_answers(payload, questions)

    async def decide_approval(self, ctx: Any, request: Any) -> "ApprovalDecision":
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
        prompt_id = self._broker.new_id("approval")  # type: ignore[union-attr]
        await self._emit(
            agui.approval_prompt(
                approval_id=prompt_id,
                tool_name=getattr(request, "tool_name", "") or "",
                action=approval_action(request),
                args_preview=approval_preview(request),
                risk=approval_risk(request),
            )
        )
        payload = await self._await_reply(prompt_id)
        return self._parse_decision(payload, prompt_id)

    # ------------------------------------------------------------------
    # Payload parsing (opaque reply dict → typed result); tolerant of shape
    # ------------------------------------------------------------------
    @staticmethod
    def _answer_text(payload: Any) -> str:
        """A free-text ``ask`` reply — accept ``{answer}`` or a bare string."""
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            val = payload.get("answer")
            if isinstance(val, str):
                return val
        return ""

    @staticmethod
    def _first_question_text(questions: Any) -> str:
        try:
            items = getattr(questions, "questions", None) or list(questions)
            return getattr(items[0], "question", "") if items else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _serialize_questions(questions: Any) -> Any:
        """Best-effort JSON-able dump of the question payload for the frontend."""
        dump = getattr(questions, "model_dump", None)
        if callable(dump):
            try:
                return dump(mode="json")
            except Exception:  # noqa: BLE001
                pass
        return None

    def _parse_answers(self, payload: Any, questions: Any) -> "AskUserQuestionAnswers":
        """Map a ``{answers:[...]}`` reply into structured answers, else empty."""
        if not isinstance(payload, dict):
            return AskUserQuestionAnswers(answers=[])
        raw = payload.get("answers")
        out: List[Any] = []
        if isinstance(raw, list):
            for a in raw:
                if not isinstance(a, dict):
                    continue
                sel = a.get("selected")
                out.append(
                    AskUserQuestionAnswer(
                        header=str(a.get("header", "")),
                        question=str(a.get("question", "")),
                        selected=[str(s) for s in sel] if isinstance(sel, list) else [],
                        free_text=str(a.get("free_text", "") or a.get("freeText", "")),
                    )
                )
        return AskUserQuestionAnswers(answers=out)

    def _parse_decision(self, payload: Any, prompt_id: str) -> "ApprovalDecision":
        """Map a ``{outcome, editedArgs?}`` reply to an ``ApprovalDecision``.

        A missing/garbled/absent reply rejects (fail-safe). ``outcome`` is
        validated against the four known values; anything else → reject.
        """
        if not isinstance(payload, dict):
            return ApprovalDecision(approval_id=prompt_id, outcome="reject")
        outcome = payload.get("outcome")
        if outcome not in ("accept", "reject", "always_allow", "always_deny"):
            outcome = "reject"
        edited = payload.get("editedArgs")
        if edited is not None and not isinstance(edited, dict):
            edited = None
        return ApprovalDecision(approval_id=prompt_id, outcome=outcome, edited_args=edited)

    # ------------------------------------------------------------------
    # Control affordances (server owns the run task; these stay inert)
    # ------------------------------------------------------------------
    def signal_interrupt(self, ctx: Any) -> None:
        """Cancel the in-flight turn. Phase 2/3: no-op (server owns the run task)."""
        return None

    def submit_steer(self, ctx: Any, text: str) -> None:
        """Turn-level steering. No-op (no mid-stream steer control yet)."""
        return None

    async def aclose(self) -> None:
        """Mark closed so a late prompt short-circuits to its safe default.

        The broker's pending futures are the *server*'s responsibility to
        ``cancel_all`` on shutdown — a per-request port doesn't own the shared
        broker, only its own emit gate.
        """
        self._closed = True


__all__ = ["AguiPort", "Sink", "DEFAULT_PROMPT_TIMEOUT_S"]
