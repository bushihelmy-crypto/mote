#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TextualPort`` — the Textual host's :class:`InteractivePort` (§2.5 / design B).

The full-screen TUI owns the asyncio loop and the keyboard, so this port carries
**none** of the terminal port's raw-stdin machinery — no ``StreamReader``, no
two-stage SIGINT handler, no parked ``_ask_waiter``. Instead:

* ``read_turn()`` awaits a single ``asyncio.Future`` (``_read_future``); the app's
  ``on_input_submitted`` resolves it via :meth:`feed_turn`. Because there is no
  ``readline()`` at all, two reads can never race — the whole single-reader
  invariant the terminal port fought for is *structurally* impossible here.
* ``ask`` / ``decide_approval`` push a modal :class:`ModalScreen` and await its
  dismissal through a callback-resolved Future (safe to call from the agent task,
  which shares the app's loop — unlike ``push_screen_wait`` these never require a
  Textual *worker* context).
* mid-turn Ctrl+C, steering, exit and prompt-restore route through the SAME driver
  hooks (``_on_interrupt`` / ``_on_steer``) and app affordances the terminal port
  uses, so the driver stays host-agnostic.

The ``_app`` is set after construction (``run_textual`` builds the app, the port,
then binds them) so the port object can exist before the app that hosts it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, List, Optional

from mote.cli.contracts.view.events import ApprovalDecision


class TextualPort:
    """Conversational port for the Textual host: Future-based reads + modal asks."""

    def __init__(self, app: Any = None):
        self._app = app
        # Driver-wired hooks (mirrors of the terminal port's contract).
        self._on_interrupt: Optional[Callable[[], Any]] = None
        self._is_turn_running: Optional[Callable[[], bool]] = None
        self._on_steer: Optional[Callable[[str], Any]] = None

        self._read_future: Optional[asyncio.Future] = None
        self._exit = False
        # Image attachments for the just-submitted turn (dragged into the prompt),
        # each ``{"token", "path", "b64", "mime"}``. Set by :meth:`feed_turn`,
        # drained once by :meth:`take_turn_images` right after the driver reads it.
        self._pending_images: list = []

    # ------------------------------------------------------------------
    # Binding + lifecycle (the app owns setup/teardown — these are inert)
    # ------------------------------------------------------------------
    def bind_app(self, app: Any) -> None:
        """Attach the live :class:`MoteApp` (post-construction wiring)."""
        self._app = app

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------
    # InteractivePort.read_turn — one Future per turn (resolved by feed_turn)
    # ------------------------------------------------------------------
    async def read_turn(self) -> Optional[str]:
        """Await the next submitted line, or ``None`` on exit.

        A fresh Future is created per turn and resolved by :meth:`feed_turn`
        (normal input) or :meth:`request_exit` (``None`` → loop ends).
        """
        if self._exit:
            return None
        # Re-entering the read means the previous turn finished — tell the app to
        # drop its "working" spinner (UI affordance, not business state).
        if self._app is not None:
            idle = getattr(self._app, "set_idle", None)
            if idle is not None:
                idle()
        loop = asyncio.get_event_loop()
        self._read_future = loop.create_future()
        try:
            return await self._read_future
        except asyncio.CancelledError:
            return None
        finally:
            self._read_future = None

    def feed_turn(self, text: str, images: Optional[List[dict]] = None) -> None:
        """Resolve the pending ``read_turn`` with *text* (called by the app).

        ``images`` carries any prompt-dragged image attachments for this turn; they
        are stashed for the driver to drain via :meth:`take_turn_images` (keeping
        ``read_turn``'s ``str`` contract intact so command detection is unchanged).
        """
        self._pending_images = images or []
        fut = self._read_future
        if fut is not None and not fut.done():
            fut.set_result(text)

    def take_turn_images(self) -> List[dict]:
        """Return and clear the image attachments staged for the last read turn."""
        images = self._pending_images
        self._pending_images = []
        return images

    def is_waiting_for_turn(self) -> bool:
        """True when a ``read_turn`` Future is pending — the app routes a submit
        as new turn input (vs. mid-turn steering) based on this.
        """
        fut = self._read_future
        return fut is not None and not fut.done()

    # ------------------------------------------------------------------
    # InputPort.ask / decide_approval — modal overlays (callback + Future)
    # ------------------------------------------------------------------
    async def ask(self, ctx: Any, question: str, options: Optional[List[str]] = None, multi: bool = False) -> str:
        """Push a :class:`QuestionScreen` and await the typed/chosen answer.

        Pure free-text is the public contract (§7); ``options`` / ``multi`` remain
        only for the ``PortHumanChannel`` degrade path. The screen now dismisses
        with a structured ``(selected, free_text)`` tuple, which we flatten back
        to the single string this legacy shape returns.
        """
        from mote.cli.consumers.textual.screens import QuestionScreen

        result = await self._push_modal(QuestionScreen(question, options, multi))
        selected, free = self._unpack(result)
        if free:
            return free
        return ", ".join(selected)

    async def ask_questions(self, ctx: Any, questions: Any) -> Any:
        """Structured multiple-choice round-trip; mirrors ``decide_approval``.

        Pushes one :class:`QuestionScreen` per question and collects the
        structured ``(selected, free_text)`` each dismisses with.
        """
        from mote.cli.consumers.textual.screens import QuestionScreen
        from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers

        out = []
        multiq = len(questions) > 1
        for q in questions:
            labels = [o.label for o in q.options]
            header = f"[{q.header}] {q.question}" if multiq else q.question
            result = await self._push_modal(QuestionScreen(header, labels, q.multiSelect))
            selected, free = self._unpack(result)
            out.append(AskUserQuestionAnswer(header=q.header, question=q.question, selected=selected, free_text=free))
        return AskUserQuestionAnswers(answers=out)

    @staticmethod
    def _unpack(result: Any) -> tuple:
        """Normalize a QuestionScreen dismissal to ``(selected: list, free: str)``."""
        if isinstance(result, tuple) and len(result) == 2:
            selected, free = result
            return list(selected or []), free or ""
        return [], ""

    async def decide_approval(self, ctx: Any, request: Any) -> Any:
        """Push an :class:`ApprovalScreen` and await the :class:`ApprovalDecision`."""
        from mote.cli.consumers.textual.screens import ApprovalScreen

        approval_id = getattr(request, "approval_id", "") or ""
        result = await self._push_modal(ApprovalScreen(request))
        if isinstance(result, ApprovalDecision):
            return result
        return ApprovalDecision(approval_id=approval_id, outcome="reject")

    async def _push_modal(self, screen: Any) -> Any:
        """Push *screen* and await its dismissal value via a callback-resolved Future.

        Unlike ``App.push_screen_wait`` (which requires an active Textual worker),
        the callback form is legal from any coroutine on the app's loop — which is
        exactly where the agent task calling ``ask_human`` runs.
        """
        if self._app is None:
            return None
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        def _dismissed(result: Any) -> None:
            if not fut.done():
                fut.set_result(result)

        self._app.push_screen(screen, _dismissed)
        return await fut

    # ------------------------------------------------------------------
    # InputPort.signal_interrupt + steer / exit / restore controls
    # ------------------------------------------------------------------
    def signal_interrupt(self, ctx: Any = None) -> None:
        """Programmatic interrupt (mirror of mid-turn Ctrl+C)."""
        if self._on_interrupt is not None:
            result = self._on_interrupt()
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def submit_steer(self, ctx: Any = None, text: str = "") -> None:
        """Forward steering *text* to the driver for the next turn (§5.3)."""
        if text and text.strip() and self._on_steer is not None:
            result = self._on_steer(text)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    def request_exit(self) -> None:
        """Signal the loop to exit and resolve any pending read with ``None``."""
        self._exit = True
        fut = self._read_future
        if fut is not None and not fut.done():
            fut.set_result(None)

    def stage_restore(self, text: str) -> None:
        """Pre-fill the app's prompt input with an interrupted turn's text."""
        if self._app is not None:
            stage = getattr(self._app, "stage_prompt", None)
            if stage is not None:
                stage(text)

    @property
    def should_exit(self) -> bool:
        return self._exit


__all__ = ["TextualPort"]
