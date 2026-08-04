#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Input ports — three *inbound* semantics over one shared base (§2.5).

The input side is classified by **how inbound arrives**, NOT by "who owns this
session" (the latter is the driver-arbitration concern of §5). All three share
:class:`InputPort` — ``ask`` (route an ``AskUserQuestion`` back to a human) and
``signal_interrupt`` (cancel / Ctrl+C) — so ``AskUserQuestion`` and interruption
mean the same thing on every platform.

* :class:`InteractivePort` — conversational (terminal / Web / IM): a clean "one
  turn" boundary the :class:`SessionDriver` can linearly ``await read_turn()``.
* :class:`BroadcastPort` — fan-in (Twitter / 公众号 / email): no turn boundary;
  each pushed message triggers one drive, routed to its owning session (§7).
* :class:`ProtocolPort` — machine-driven (Symphony) via structured RPC; payload
  is not natural language (§6). Semantically near broadcast (push → one turn).

A ``ProtocolPort`` is a member of the *inbound* axis, not a "fourth endpoint
type": a machine consumer = ``ProtocolPort`` (in) + ``AppServerProjector→machine``
(out), both mounted on one session, co-equal with human consumers (§2.5 note).

This is a LEAF interface module: it imports only ``typing``, so it can be
imported from any host (terminal / Web / IM / machine) without a cycle — the
input contract is platform-agnostic by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Protocol, runtime_checkable

from mote.contracts.events.envelope import JsonValue

if TYPE_CHECKING:  # avoid a runtime import cycle (view.events → common) — types only
    from mote.contracts.interaction import ApprovalRequest, AskUserQuestionAnswers, AskUserQuestionInput
    from mote.contracts.interaction.handoff import DriverHandoffHandle, HandoffRequest, HumanHandoffOutcome
    from mote.contracts.surface import LiveSurfaceSession
    from mote.product.presentation.events.events import ApprovalDecision


class DriverControlDisposition(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_PENDING = "already_pending"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class DriverControlReceipt:
    disposition: DriverControlDisposition


@dataclass(frozen=True)
class DriverControlBinding:
    interrupt: Callable[[], DriverControlReceipt]
    turn_running: Callable[[], bool]
    steer: Callable[[str], DriverControlReceipt]


@runtime_checkable
class InputPort(Protocol):
    """Public face: normalize external input into something the core can drive.

    The three operations every inbound semantics shares. ``ctx`` is an opaque
    per-session handle (the driver passes whatever it needs to correlate the
    request); ports stay agnostic to its shape.
    """

    def bind_driver_control(self, binding: DriverControlBinding) -> None: ...

    async def start(self) -> None: ...

    async def aclose(self) -> None: ...

    def take_turn_images(self) -> list[Mapping[str, JsonValue]]: ...

    def request_exit(self) -> None: ...

    async def open_handoff(
        self,
        request: "HandoffRequest",
        handle: "DriverHandoffHandle",
        surface: "LiveSurfaceSession | None" = None,
    ) -> "HumanHandoffOutcome": ...

    async def ask(self, ctx: object, question: str) -> str:
        """Route a free-text question to a human and return their answer.

        Pure free-text only. All option / multi-select logic lives in
        ``ask_questions`` — this stays a plain 2-arg contract on every platform.
        """
        ...

    async def ask_questions(self, ctx: object, questions: "AskUserQuestionInput") -> "AskUserQuestionAnswers":
        """Structured multiple-choice round-trip; mirrors ``decide_approval``.

        The structured sibling of ``ask``: the display side (``QuestionAsked``)
        flows *down* to consumers, the structured answer flows *back up* here.
        Each returned answer keeps ``selected`` labels and ``free_text`` in
        separate fields, so a numeric or multi-line free-text answer is never
        misread as an option index. A port that can't gate should return empty
        or default answers rather than block, so the contract holds everywhere.
        """
        ...

    async def decide_approval(self, ctx: object, request: "ApprovalRequest") -> "ApprovalDecision":
        """Route a gated action to a human and return their structured decision.

        The inbound counterpart of the ``ApprovalRequested`` ViewEvent: the display
        side flows *down* to consumers, the decision flows *back up* here (symmetric
        with ``ask`` returning a ``str``). A port that can't gate — a broadcast /
        machine transport — should return a reject/deny decision or its policy
        default rather than block, so the contract holds on every platform.
        """
        ...

    def signal_interrupt(self, ctx: object) -> DriverControlReceipt:
        """Cancel the in-flight turn (Ctrl+C / explicit cancel)."""
        ...

    def submit_steer(self, ctx: object, text: str) -> DriverControlReceipt:
        """Queue *steering* input to fold into the **next** turn (§5.3).

        Turn-level steering, NOT a mid-turn interrupt: the text is captured now
        (e.g. typed while a turn is in flight, or pushed by a Web steer control)
        and the driver drains it at the next turn boundary. Symmetric with
        ``signal_interrupt`` — a public inbound entry point the port forwards to
        the driver. A port with no steering affordance may no-op.
        """
        ...


@runtime_checkable
class InteractivePort(InputPort, Protocol):
    """Conversational: terminal / Web / IM — a clean per-turn boundary.

    The driver can ``await read_turn()`` linearly; ``None`` signals end-of-input
    (EOF / explicit exit), terminating the loop.
    """

    async def read_turn(self) -> Optional[str]:
        """Return the next turn's input, or ``None`` when input is exhausted."""
        ...

    def stage_restore(self, text: str) -> None: ...


@runtime_checkable
class BroadcastPort(InputPort, Protocol):
    """Fan-in: Twitter / 公众号 / email — no turn boundary, push-triggered.

    Each inbound message triggers one drive; a :class:`SessionRouter` (§7) maps
    it to its owning session. The port pushes into ``on_message`` rather than
    being polled.
    """

    def subscribe(self, on_message: Callable[[object], Awaitable[None]]) -> None:
        """Register the callback invoked once per inbound message."""
        ...


@runtime_checkable
class ProtocolPort(InputPort, Protocol):
    """Machine-driven (Symphony) via structured RPC; payload is not NL (§6).

    Driven by an external program with ``turn/start``-style requests; the port
    decodes each and dispatches into ``on_request``, returning a structured
    result. Semantically near broadcast (one request → one turn).
    """

    def serve(self, on_request: Callable[[object], Awaitable[object]]) -> None:
        """Register the request handler the transport feeds decoded RPCs into."""
        ...


__all__ = ["DriverControlBinding", "InputPort", "InteractivePort", "BroadcastPort", "ProtocolPort"]
