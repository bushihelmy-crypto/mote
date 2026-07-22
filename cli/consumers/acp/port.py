#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``AcpPort`` — the ACP input half (turn boundary + HITL over a live JSON-RPC link).

Unlike AG-UI (a *stateless* request/response where the reply arrives on a
separate HTTP request, so the port must rendezvous through a cross-request
``PromptBroker``), ACP is a **stateful bidirectional** stdio JSON-RPC connection:
the agent can send the client a ``session/request_permission`` *request* and
await its response inline on the same link. So :meth:`decide_approval` blocks
directly on an injected ``request_permission`` callable (the server binds it to
``endpoint.request("session/request_permission", params)``) — no broker, no
correlation id minting, no back-channel plumbing.

The turn boundary is likewise trivial: one ``session/prompt`` carries the turn's
prompt text, so :meth:`read_turn` yields it once then ``None`` (the server drives
one turn per prompt request, mirroring AG-UI's ``/run`` boundary).

ACP has **no native free-text / structured-question** client method (its only
agent→client request is ``session/request_permission``), so :meth:`ask` /
:meth:`ask_questions` fall back to safe non-blocking defaults (empty answers) —
a turn never wedges on a prompt the protocol can't deliver. The one interactive
round-trip ACP *does* model — permission — is fully wired.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from mote.cli.consumers._wire import acp
from mote.cli.view.approval import approval_action, approval_preview, approval_risk
from mote.common.logs import logger

if TYPE_CHECKING:  # types only — avoid a runtime import cycle
    from mote.cli.contracts.view.events import ApprovalDecision
    from mote.common.schema import AskUserQuestionAnswers, AskUserQuestionInput

#: An async request sender the server injects: ``(method, params) -> result``.
#: The server binds this to the JSON-RPC endpoint's ``request`` so the port can
#: send a client-bound request (``session/request_permission``) and await the
#: reply on the same live connection. Returns ``None`` on transport failure.
RequestFn = Callable[[str, Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]

#: The one agent→client request method ACP defines for HITL — the port sends it
#: and blocks on the inline reply (server imports this so both agree on the name).
M_REQUEST_PERMISSION = "session/request_permission"

#: Seconds to wait for the client's permission reply before the safe default.
#: Generous — a human is in the loop — but bounded so a client that goes silent
#: mid-request can't wedge the resident session forever.
DEFAULT_PERMISSION_TIMEOUT_S = 600.0


class AcpPort:
    """One session's inbound edge for ACP ``session/prompt`` turns.

    Constructed per prompt turn with the turn's ``text``; the server also injects
    ``request`` (the live JSON-RPC request sender) + ``session_id`` so a gated
    tool call can send ``session/request_permission`` and block on the reply.
    Implements the
    :class:`~mote.cli.contracts.interface.ports.InputPort` protocol structurally
    (duck-typed — no nominal base, matching the leaf-interface contract).
    """

    def __init__(
        self,
        text: str = "",
        *,
        session_id: str = "",
        request: Optional[RequestFn] = None,
        timeout_s: float = DEFAULT_PERMISSION_TIMEOUT_S,
    ) -> None:
        self._text: Optional[str] = text
        self._session_id = session_id
        self._request = request
        self._timeout_s = timeout_s
        self._closed = False

    # ------------------------------------------------------------------
    # Turn boundary
    # ------------------------------------------------------------------
    async def read_turn(self) -> Optional[str]:
        """Return the prompt's injected text once, then ``None`` (turn done).

        ACP drives exactly one turn per ``session/prompt`` request, so the first
        read yields the prompt text and the next signals end-of-input — the
        driver runs a single turn and unwinds, then the server replies to the
        ``session/prompt`` request with the turn's ``stopReason``.
        """
        text, self._text = self._text, None
        return text

    # ------------------------------------------------------------------
    # HITL — permission is ACP's one native interactive round-trip
    # ------------------------------------------------------------------
    async def ask(self, ctx: Any, question: str, options: Optional[List[str]] = None, multi: bool = False) -> str:
        """Free-text question — no ACP client method delivers it, so return empty.

        ACP models only permission as an agent→client request; a free-text ask
        has nowhere to go on the wire, so we return empty rather than block the
        turn on an answer that can't arrive (``options``/``multi`` are accepted
        only for the ``PortHumanChannel`` degrade path's signature).
        """
        logger.debug("AcpPort.ask: ACP has no free-text prompt channel; returning empty answer")
        return ""

    async def ask_questions(self, ctx: Any, questions: "AskUserQuestionInput") -> "AskUserQuestionAnswers":
        """Structured multiple-choice — likewise undeliverable over ACP → empty.

        No ``session/request_*`` method carries a structured question, so this
        returns empty answers (non-blocking), matching the ``InputPort`` contract
        that a port which can't gate returns defaults rather than blocking.
        """
        from mote.common.schema import AskUserQuestionAnswers

        return AskUserQuestionAnswers(answers=[])

    async def decide_approval(self, ctx: Any, request: Any) -> "ApprovalDecision":
        """Gated action: send ``session/request_permission``, await the client's pick.

        The engine hands a language-neutral ``ApprovalRequest``; we build the ACP
        ``RequestPermissionRequest`` (the gated tool call as a ``ToolCallUpdate``
        + the four standard :func:`~mote.cli.consumers._wire.acp.permission_options`)
        and send it as a request on the live link, awaiting the
        ``RequestPermissionResponse``. A ``selected`` outcome maps its ``optionId``
        back to an :class:`ApprovalDecision` outcome via
        :data:`~mote.cli.consumers._wire.acp.PERM_KIND_TO_OUTCOME`; a ``cancelled``
        outcome, a missing/garbled reply, a timeout, or no wired ``request``
        callable all reject (fail-safe — the answer to "may I run this?" without a
        clear human yes is no).
        """
        from mote.cli.contracts.view.events import ApprovalDecision

        if self._request is None or self._closed:
            logger.debug("AcpPort.decide_approval: no live link; rejecting (fail-safe)")
            return ApprovalDecision(approval_id="", outcome="reject")

        tool_name = getattr(request, "tool_name", "") or ""
        params: Dict[str, Any] = {
            "sessionId": self._session_id,
            "toolCall": acp.tool_call_update_for_permission(
                tool_call_id=self._permission_tool_id(request),
                tool_name=tool_name,
                title=approval_action(request),
            ),
            "options": acp.permission_options(),
        }
        # Surface the localized preview/risk so a client that renders extra
        # context (beyond the tool call itself) has it; harmless if ignored.
        preview = approval_preview(request)
        if preview:
            params["_meta"] = {"preview": preview, "risk": approval_risk(request)}

        reply = await self._send(params)
        return self._parse_outcome(reply)

    # ------------------------------------------------------------------
    # Wire round-trip + reply parsing
    # ------------------------------------------------------------------
    async def _send(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send the permission request, bounded by the timeout; ``None`` on failure."""
        if self._request is None:
            return None
        try:
            return await asyncio.wait_for(self._request(M_REQUEST_PERMISSION, params), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            logger.info("AcpPort: permission request timed out; using safe default (reject)")
            return None
        except asyncio.CancelledError:
            # Connection teardown / session cancel — fall back, don't propagate a
            # cancellation into the turn (the run is unwinding anyway).
            return None
        except Exception as exc:  # noqa: BLE001 — a dead link must not crash the turn
            logger.warning(f"AcpPort: permission request failed: {exc}")
            return None

    @staticmethod
    def _permission_tool_id(request: Any) -> str:
        """Correlate the permission to its tool call if the request carries an id."""
        for attr in ("tool_use_id", "approval_id", "call_id"):
            val = getattr(request, attr, None)
            if isinstance(val, str) and val:
                return val
        return "pending"

    @staticmethod
    def _parse_outcome(reply: Optional[Dict[str, Any]]) -> "ApprovalDecision":
        """Map a ``RequestPermissionResponse`` to an ``ApprovalDecision`` (reject-default).

        Response shape: ``{outcome: {outcome:"selected", optionId} | {outcome:"cancelled"}}``.
        A ``selected`` id maps through :data:`PERM_KIND_TO_OUTCOME`; anything else
        (cancelled / missing / unknown id) rejects.
        """
        from mote.cli.contracts.view.events import ApprovalDecision

        if not isinstance(reply, dict):
            return ApprovalDecision(approval_id="", outcome="reject")
        outcome = reply.get("outcome")
        if isinstance(outcome, dict) and outcome.get("outcome") == "selected":
            option_id = outcome.get("optionId")
            mapped = acp.PERM_KIND_TO_OUTCOME.get(option_id) if isinstance(option_id, str) else None
            if mapped is not None:
                return ApprovalDecision(approval_id="", outcome=mapped)
        return ApprovalDecision(approval_id="", outcome="reject")

    # ------------------------------------------------------------------
    # Control affordances (server owns the run task; these stay inert)
    # ------------------------------------------------------------------
    def signal_interrupt(self, ctx: Any) -> None:
        """Cancel the in-flight turn. The server maps ``session/cancel`` to this;
        Phase 4 leaves the actual cancel to the server's run-task ownership."""
        return None

    def submit_steer(self, ctx: Any, text: str) -> None:
        """Turn-level steering. No-op (ACP has no mid-turn steer method)."""
        return None

    async def aclose(self) -> None:
        """Mark closed so a late permission request short-circuits to reject."""
        self._closed = True


__all__ = ["AcpPort", "RequestFn", "DEFAULT_PERMISSION_TIMEOUT_S"]
