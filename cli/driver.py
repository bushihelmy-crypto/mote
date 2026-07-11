#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SessionDriver`` — the thin orchestration loop + driver-权 arbitration (§2.6).

The §8 successor of the ``Repl`` god object, reduced to pure orchestration: it
does **no I/O** (the :class:`InteractivePort` owns stdin/SIGINT) and **no
rendering** (the :class:`BaseProjector` → consumers own that). Per §2.6 it adds
one thing over the old loop — it is the sole arbiter of the per-session driver
权 (a ``_turn_lock``): one initiator per turn.

The defining change from ``Repl`` (§0.2 / §8): it **never reads
``context.messages``**. An assistant reply reaches the user the same way every
other observer sees it — ``MessageAppendedEvent`` → ``ViewProjector`` →
consumer. The privileged path is gone; humans and machines are symmetric
downstreams of the one ``AgentEvent`` truth source.

The driver also exposes the small host surface the command registry dispatches
against (``notice`` / ``request_exit`` / agent-lifecycle), routing command output
through ``projector.deliver_sync(Notice(...))`` so it renders on every consumer
(§2.7), not raw stdout.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Optional

from mote.cli import backend
from mote.cli.commands.registry import CommandRegistry, default_registry
from mote.cli.contracts.base import BaseProjector
from mote.cli.contracts.view import (
    ErrorRaised,
    MediaBlock,
    MessageBlockCompleted,
    Notice,
    SessionListItem,
    SessionListShown,
    TranscriptCleared,
)
from mote.cli.io.human_channel import PortHumanChannel
from mote.common.logs import logger


def _format_turn_error(err: BaseException) -> str:
    """Render a turn's exception into a concise one/two-line message.

    Typed ``MoteError`` subclasses carry a clean ``message`` + optional
    upstream ``status_code``; anything else falls back to ``Type: str(err)``.
    """
    cls = type(err).__name__
    detail = str(err).strip() or repr(err)
    status = getattr(err, "status_code", None)
    if status is not None:
        return f"{cls} (HTTP {status}): {detail}"
    return f"{cls}: {detail}"


class SessionDriver:
    """Drive one session: arbitrate the turn lock, route I/O through ports/consumers."""

    def __init__(
        self,
        control: Any,
        agent_id: str,
        role: Any,
        *,
        port: Any,
        projector: BaseProjector,
        commands: Optional[CommandRegistry] = None,
        role_factory: Optional[Any] = None,
        quiescent_poll_interval: float = 0.05,
    ):
        self._control = control
        self._agent_id = agent_id
        self._role = role
        self._port = port
        self._projector = projector
        self._commands = commands or default_registry()
        self._role_factory = role_factory
        self._quiescent_poll_interval = quiescent_poll_interval

        self._exit = False
        self._turn_lock = asyncio.Lock()
        self._running_turn = False
        self._current_input: Optional[str] = None
        self._subscribed_buses: list = []
        self._last_sessions: list = []  # cached for index-based /resume
        # Steering queue (§5.3): text captured while a turn is in flight is
        # drained at the *next* turn boundary — turn-level steering, NOT a
        # step-level mid-turn interrupt (that lives in the framework loop).
        self._steer_queue: deque[str] = deque()

        # Wire the port's interrupt/turn-state/steer hooks to this driver.
        for attr, value in (
            ("_on_interrupt", self._interrupt_current_turn),
            ("_is_turn_running", lambda: self._running_turn),
            ("_on_steer", self._enqueue_steer),
        ):
            if hasattr(self._port, attr):
                setattr(self._port, attr, value)

    # ------------------------------------------------------------------
    # Main loop (§2.6 pseudocode)
    # ------------------------------------------------------------------
    async def run(self) -> None:
        start = getattr(self._port, "start", None)
        if start is not None:
            await start()
        self._subscribe_projector(self._role)
        backend.bind_human_channel(self._role, PortHumanChannel(self._port))
        self._announce_tools()
        self._control.start()
        try:
            while not self._exit:
                text = await self._port.read_turn()
                if self._exit or text is None:
                    break
                # Drain any prompt-dragged image attachments for this turn (only
                # the Textual port has them); a command turn discards them.
                images = self._take_turn_images()
                if not text.strip() and not images:
                    continue
                if text.strip() and self._commands.is_command(text):
                    await self._commands.handle(self, text)
                    continue
                await self._run_turn(self._merge_steer(text), images=images)
        finally:
            await self._teardown()

    def _announce_tools(self) -> None:
        """Flag how many built-in tools the session opened with (§ startup badge).

        Rendered once on open via a ``Notice`` so it lands on every consumer
        (terminal / textual / future web) the same way, right under the banner.
        The ⚑ glyph mirrors the per-turn system-reminder flag the consumer already
        uses for injected context, so the human reads "this is framework chrome".

        Only the built-in tool count belongs here: under provider-native tool-use
        the tools are bound once at session open and shipped via the API ``tools=``
        param, so their count is a genuine startup fact. MCP servers, by contrast,
        connect lazily and surface their tools per-turn in the ``<system-reminder>``
        catalog — they are not part of the one-time startup load, so counting them
        here would misreport a load that never happened.
        """
        builtin = backend.role_tool_count(self._role)
        if not builtin:
            return
        self.notice(f"\u2691 已加载 {builtin} 个工具")

    def _take_turn_images(self) -> list:
        """Drain the port's staged image attachments for this turn (Textual only).

        Only the Textual port stages prompt-dragged images; every other port
        returns nothing, so the driver's per-turn image handling degrades to a
        no-op text turn everywhere else.
        """
        take = getattr(self._port, "take_turn_images", None)
        return take() if take is not None else []

    async def _run_turn(self, text: str, images: Optional[list] = None) -> None:
        """Send one input and await quiescence; output flows via projector→consumer.

        *images* (Textual prompt-dragged attachments, each ``{"b64","path","mime"}``)
        ride along as ``metadata[IMAGES]`` on the ``UserMessage`` so the LLM sees
        them as multimodal blocks, and each is surfaced as a ``MediaBlock`` so the
        dragged image renders in the transcript on every consumer.
        """
        self._current_input = text
        images = images or []
        async with self._turn_lock:  # ★ driver 权: one initiator per turn
            self._running_turn = True
            try:
                # Surface the user's own turn as a ViewEvent so it lands in the
                # transcript on every consumer (terminal / textual / future web),
                # symmetric with the assistant reply — the driver is the one
                # host-agnostic place that holds the (steer-merged) prompt text.
                await self._projector.deliver(MessageBlockCompleted(role="user", markdown=text, streamed=False))
                for img in images:
                    await self._projector.deliver(
                        MediaBlock(
                            media_kind="image",
                            ref=img.get("path", "") or "",
                            mime=img.get("mime"),
                            alt=img.get("path", "") or "image",
                        )
                    )
                msg = backend.turn_message(text, [img["b64"] for img in images] if images else None)
                self._control.send_input(self._agent_id, msg)
                await asyncio.sleep(0)  # let the driver pick up the wake
                while not self._control.quiescent():
                    await asyncio.sleep(self._quiescent_poll_interval)
            finally:
                self._running_turn = False
                self._current_input = None
        # A turn that ended in ERRORED leaves no assistant reply; surface the
        # failure as an ErrorRaised ViewEvent (not by reading context.messages).
        runtime = self._control.get_runtime(self._agent_id)
        err = getattr(runtime, "last_error", None) if runtime is not None else None
        if err is not None:
            await self._projector.deliver(ErrorRaised(text=_format_turn_error(err)))

    def _interrupt_current_turn(self) -> None:
        """Mid-turn Ctrl+C: stage the prompt for restore, then interrupt the turn."""
        if self._current_input and hasattr(self._port, "stage_restore"):
            self._port.stage_restore(self._current_input)
        asyncio.ensure_future(self._control.interrupt(self._agent_id))

    # ------------------------------------------------------------------
    # Steering (§5.3): turn-boundary input queue
    # ------------------------------------------------------------------
    def _enqueue_steer(self, text: str) -> None:
        """Producer side: stash steering text (from the port) for the next turn.

        The port calls this via its ``_on_steer`` hook. Enqueueing never
        preempts the in-flight turn — the text is merged in at the next turn
        boundary by :meth:`_merge_steer` (§5.3 turn-level steering).
        """
        if text and text.strip():
            self._steer_queue.append(text.strip())

    def _merge_steer(self, text: str) -> str:
        """Drain any queued steering ahead of *text*, oldest first, one per line.

        Consumed at the turn boundary so a steer captured during the previous
        turn lands as context for the upcoming one. Returns the combined prompt.
        """
        if not self._steer_queue:
            return text
        queued = []
        while self._steer_queue:
            queued.append(self._steer_queue.popleft())
        queued.append(text)
        return "\n".join(queued)

    # ------------------------------------------------------------------
    # Projector subscription (per role bus)
    # ------------------------------------------------------------------
    def _subscribe_projector(self, role: Any) -> None:
        """Subscribe the shared projector to *role*'s event bus (once per bus)."""
        bus = backend.role_event_bus(role)
        if bus is None or bus in self._subscribed_buses:
            return
        try:
            bus.subscribe(self._projector)
            self._subscribed_buses.append(bus)
        except Exception as exc:  # noqa: BLE001 — rendering is best-effort
            logger.warning(f"SessionDriver: projector subscribe failed: {exc}")

    def _unsubscribe_projector(self) -> None:
        for bus in self._subscribed_buses:
            try:
                bus.unsubscribe(self._projector)
            except Exception:  # noqa: BLE001
                pass
        self._subscribed_buses = []

    async def _teardown(self) -> None:
        self._unsubscribe_projector()
        aclose = getattr(self._port, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._control.stop()
        except Exception as exc:  # noqa: BLE001 — best-effort shutdown
            logger.warning(f"SessionDriver: control.stop() failed: {exc}")
        cleanup = backend.role_cleanup(self._role)
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"SessionDriver: role.cleanup() failed: {exc}")

    # ==================================================================
    # Command host surface (the ``ctx`` the CommandRegistry dispatches on)
    # ==================================================================
    def notice(self, text: str, level: str = "info") -> None:
        """Render a command notice on every consumer (§2.7), not raw stdout."""
        self._projector.deliver_sync(Notice(text=text, level=level))

    def help_text(self) -> str:
        return self._commands.help_text()

    def request_exit(self) -> None:
        self._exit = True
        request = getattr(self._port, "request_exit", None)
        if request is not None:
            request()

    def clear_conversation(self) -> int:
        """Clear the active agent's conversation — history + rendered transcript.

        Drops the stored message history on the Role's ContextManager (so the
        next turn starts fresh) and emits a ``TranscriptCleared`` ViewEvent so
        every consumer wipes what it has shown. Returns the number of messages
        that were cleared (for the handler's notice).
        """
        cleared = backend.clear_messages(self._role)
        self._projector.deliver_sync(TranscriptCleared())
        return cleared

    @property
    def current_agent_id(self) -> str:
        return self._agent_id

    def active_agents(self) -> list:
        """Return ``[(agent_id, name, status), ...]`` for the live control plane."""
        out = []
        for agent_id, runtime in self._control.runtimes().items():
            name = backend.runtime_name(runtime)
            status = self._control.get_status(agent_id).value
            out.append((agent_id, name, status))
        return out

    def _set_active(self, agent_id: str) -> bool:
        runtime = self._control.get_runtime(agent_id)
        if runtime is None:
            return False
        self._agent_id = agent_id
        self._role = backend.runtime_role(runtime)
        return True

    @staticmethod
    def _resolve_ref(ref: str, ids: list) -> Optional[str]:
        """Resolve a reference against an ordered id list: index | exact | unique-prefix."""
        ref = ref.strip()
        if ref.isdigit():
            i = int(ref)
            return ids[i] if 0 <= i < len(ids) else None
        if ref in ids:
            return ref
        prefixed = [i for i in ids if i.startswith(ref)]
        return prefixed[0] if len(prefixed) == 1 else None

    def _resolve_agent(self, ref: str) -> Optional[str]:
        agents = list(self._control.runtimes().items())
        hit = self._resolve_ref(ref, [aid for aid, _ in agents])
        if hit is not None:
            return hit
        named = [aid for aid, rt in agents if backend.runtime_name(rt) == ref.strip()]
        return named[0] if len(named) == 1 else None

    def switch_agent(self, ref: str) -> Optional[tuple]:
        """Switch the active agent. Returns ``(agent_id, name)`` or ``None``."""
        agent_id = self._resolve_agent(ref)
        if agent_id is None or not self._set_active(agent_id):
            return None
        runtime = self._control.get_runtime(agent_id)
        name = backend.runtime_name(runtime)
        return agent_id, name

    def _make_role(
        self, *, name: str = "Assistant", session_id: Optional[str] = None, agent_type: Optional[str] = None
    ):
        if self._role_factory is None:
            return None
        return self._role_factory(name=name, session_id=session_id, agent_type=agent_type)

    def adopt_role(self, role: Any, *, switch: bool = True, root: bool = False) -> str:
        """Wrap *role* into a runtime, add it to the plane, wire console + bus."""
        backend.bind_human_channel(role, PortHumanChannel(self._port))
        self._subscribe_projector(role)
        runtime = backend.wrap_runtime(role)
        self._control.add_agent(runtime, root=root)
        sid = backend.role_session_id(role)
        if switch:
            self._set_active(sid)
        return sid

    def new_agent(self, name: str = "Assistant") -> Optional[str]:
        role = self._make_role(name=name)
        if role is None:
            return None
        return self.adopt_role(role, switch=True)

    def list_agent_types(self) -> list:
        """List available spawnable agent types as ``[(name, description), ...]``."""
        return backend.list_agent_types()

    def spawn_agent_type(self, agent_type: str, name: str = "") -> tuple:
        """Spawn a typed agent and switch to it. Returns ``(session_id, name)``.

        An unknown/unavailable type returns ``(None, message)`` so the command
        handler can surface the failure.
        """
        role = self._make_role(name=name or agent_type, agent_type=agent_type)
        if role is None:
            return None, f"unknown/unavailable agent type '{agent_type}'"
        sid = self.adopt_role(role, switch=True)
        return sid, name or agent_type

    def fork_current(self) -> Optional[str]:
        """Fork the current role's session into an independent sibling agent."""
        forked = backend.fork_role(self._role)
        if forked is None:
            return None
        return self.adopt_role(forked, switch=True)

    def list_resumable_sessions(self) -> list:
        """List resumable sessions (newest first), caching for index-based resume."""
        try:
            sessions = backend.list_sessions(self._role)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SessionDriver: list_sessions failed: {exc}")
            sessions = []
        self._last_sessions = sessions
        return sessions

    def show_sessions(self) -> int:
        """Render resumable sessions as a structured ``SessionListShown`` event.

        Replaces the old free-text ``Notice`` blob: the driver picks index/label/
        preview once, every consumer renders natively (terminal → numbered table,
        structured host → the array verbatim). ``index`` mirrors the cache order so
        ``/resume <index>`` stays valid. Returns the row count.
        """
        sessions = self.list_resumable_sessions()
        items = []
        for i, info in enumerate(sessions):
            label = getattr(info, "title", "") or getattr(info, "last_prompt", "") or getattr(info, "preview", "")
            label = (label or "(no preview)").replace("\n", " ")[:60]
            modified = getattr(info, "modified", "") or ""
            items.append(
                SessionListItem(
                    session_id=getattr(info, "session_id", "") or "",
                    label=label,
                    updated_at=modified[:19] if modified else None,
                    preview=(getattr(info, "preview", "") or "").replace("\n", " ")[:60],
                    index=i,
                )
            )
        self._projector.deliver_sync(SessionListShown(items=items, title="Sessions (newest first)"))
        return len(items)

    def _resolve_session_ref(self, ref: str) -> Optional[str]:
        ref = ref.strip()
        if ref.isdigit():
            i = int(ref)
            return self._last_sessions[i].session_id if 0 <= i < len(self._last_sessions) else None
        hit = self._resolve_ref(ref, [s.session_id for s in self.list_resumable_sessions()])
        if hit is not None:
            return hit
        return ref if len(ref) >= 8 else None

    def resume_session_ref(self, ref: str) -> tuple:
        """Resume a session by reference. Returns ``(ok, message)``."""
        sid = self._resolve_session_ref(ref)
        if sid is None:
            return False, f"no session matching '{ref}'"
        if sid in self._control.runtimes():
            self._set_active(sid)
            return True, f"switched to already-loaded session {sid[:8]}"
        role = self._make_role(session_id=sid)
        if role is None:
            return False, "cannot resume sessions here"
        try:
            ok = backend.resume_role(role)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SessionDriver: resume_session failed: {exc}")
            return False, f"failed to resume {sid[:8]}"
        if not ok:
            return False, f"no rollout for {sid[:8]}"
        self.adopt_role(role, switch=True)
        return True, f"resumed session {sid[:8]}"


__all__ = ["SessionDriver"]
