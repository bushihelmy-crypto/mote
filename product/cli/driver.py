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
from uuid import uuid4

from mote.contracts.ports.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.product.cli import backend
from mote.product.cli.commands.registry import CommandRegistry, default_registry
from mote.product.cli.contracts.base import BaseProjector
from mote.product.cli.contracts.view import (
    ErrorRaised,
    MediaBlock,
    MessageBlockCompleted,
    Notice,
    SessionListItem,
    SessionListShown,
    TranscriptCleared,
)
from mote.product.cli.io.human_channel import PortHumanChannel
from mote.product.i18n import keys as K
from mote.product.i18n import t
from mote.runtime.events import TelemetryBinding, TelemetryHandle
from mote.runtime.lifecycle import LifecyclePhase, LifecycleStack
from mote.runtime.logging import logger


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
        scheduler: Optional[Any] = None,
        engine: Optional[Any] = None,
        agent_catalog: Optional[Any] = None,
        quiescent_poll_interval: float = 0.05,
    ):
        self._control = control
        self._agent_id = agent_id
        self._role = role
        self._port = port
        self._projector = projector
        self._commands = commands or default_registry()
        self._role_factory = role_factory
        # Optional background scheduler (duck-typed ``start()`` / ``async stop()``);
        # e.g. a CronService firing scheduled prompts into this live session. Kept
        # cron-agnostic — the driver only owns its lifecycle, never imports cron.
        self._scheduler = scheduler
        self._engine = engine
        self._agent_catalog = agent_catalog
        self._quiescent_poll_interval = quiescent_poll_interval
        self._teardown_lifecycle = LifecycleStack()
        self._teardown_prepared = False

        self._exit = False
        self._turn_lock = asyncio.Lock()
        self._running_turn = False
        self._current_input: Optional[str] = None
        self._telemetry_handles: dict[Any, TelemetryHandle] = {}
        self._telemetry_identity = TelemetryIdentity(f"mote.product.cli.session_driver.{uuid4().hex}")
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
        await self._subscribe_projector(self._role)
        backend.bind_human_channel(self._role, PortHumanChannel(self._port))
        self._announce_tools()
        self._control.start()
        if self._scheduler is not None:
            try:
                self._scheduler.start()
            except Exception as exc:  # noqa: BLE001 — scheduling is best-effort
                logger.warning(f"SessionDriver: scheduler.start() failed: {exc}")
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

        Deferred (search-to-enable) tools *are* part of that startup load — they
        are bound at session open, only their schema is withheld until searched —
        so they count toward the total, and the badge annotates how many of it
        start deferred (e.g. "loaded 17 tools (8 deferred)").
        """
        builtin = backend.role_tool_count(self._role)
        if not builtin:
            return
        deferred = backend.role_deferred_tool_count(self._role)
        self.notice("\u2691 " + t(K.DRIVER_TOOLS_LOADED, count=builtin, deferred=deferred))

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
                await self._subscribe_projector(self._role)
                # Surface the user's own turn as a ViewEvent so it lands in the
                # transcript on every consumer (terminal / textual / future web),
                # symmetric with the assistant reply — the driver is the one
                # host-agnostic place that holds the (steer-merged) prompt text.
                # Build the backend message FIRST so the view event carries its
                # stable ``id``: the same id lands in ``context.messages``
                # (``send_input`` doesn't re-id), so a UserMessageRow can later be
                # mapped back to the exact history message for a react-unit delete.
                msg = backend.turn_message(text, [img["b64"] for img in images] if images else None)
                await self._projector.deliver(
                    MessageBlockCompleted(role="user", markdown=text, streamed=False, message_id=msg.id)
                )
                for img in images:
                    await self._projector.deliver(
                        MediaBlock(
                            media_kind="image",
                            ref=img.get("path", "") or "",
                            mime=img.get("mime"),
                            alt=img.get("path", "") or "image",
                        )
                    )
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
    # Projector subscription (per Role telemetry runtime)
    # ------------------------------------------------------------------
    async def _subscribe_projector(self, role: Any) -> None:
        """Subscribe the shared projector once per Role telemetry runtime."""
        telemetry = backend.role_telemetry(role)
        if telemetry is None or telemetry in self._telemetry_handles:
            return
        try:
            handle = await telemetry.subscribe(
                TelemetryBinding(
                    TelemetrySubscriptionSpec(
                        identity=self._telemetry_identity,
                        capacity=4096,
                        overflow=TelemetryOverflow.DROP_OLDEST,
                    ),
                    self._projector,
                )
            )
            self._telemetry_handles[telemetry] = handle
        except Exception as exc:  # noqa: BLE001 — rendering is best-effort
            logger.warning(f"SessionDriver: projector subscribe failed: {exc}")

    async def _unsubscribe_projector(self) -> None:
        for handle in tuple(self._telemetry_handles.values()):
            try:
                await handle.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._telemetry_handles.clear()

    async def _teardown(self) -> None:
        self._prepare_teardown_lifecycle()
        await self._teardown_lifecycle.aclose()

    def _prepare_teardown_lifecycle(self) -> None:
        if self._teardown_prepared:
            return
        self._teardown_prepared = True
        lifecycle = self._teardown_lifecycle
        if self._engine is not None:
            lifecycle.register_close(
                "engine",
                self._engine.aclose,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        else:
            cleanup = backend.role_cleanup(self._role)
            if cleanup is not None:
                lifecycle.register_close(
                    "role",
                    cleanup,
                    phase=LifecyclePhase.CLOSE_RESOURCES,
                )
        lifecycle.register_close(
            "control-plane",
            self._control.stop,
            phase=LifecyclePhase.CLOSE_RESOURCES,
        )
        lifecycle.register_close(
            "projector",
            self._projector.aclose,
            phase=LifecyclePhase.CLOSE_RESOURCES,
        )
        port_close = getattr(self._port, "aclose", None)
        if port_close is not None:
            lifecycle.register_close(
                "input-port",
                port_close,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        if self._scheduler is not None:
            lifecycle.register_close(
                "scheduler",
                self._scheduler.stop,
                phase=LifecyclePhase.CLOSE_RESOURCES,
            )
        lifecycle.register_close(
            "event-subscriptions",
            self._unsubscribe_projector,
            phase=LifecyclePhase.STOP_PRODUCERS,
        )

    # ==================================================================
    # Command host surface (the ``ctx`` the CommandRegistry dispatches on)
    # ==================================================================
    def notice(self, text: str, level: str = "info") -> None:
        """Render a command notice on every consumer (§2.7), not raw stdout."""
        self._projector.deliver_sync(Notice(text=text, level=level))

    def help_text(self) -> str:
        return self._commands.help_text()

    def usage_report(self) -> str:
        """Session cost + provider rate-limit quota, for the ``/usage`` command."""
        return backend.usage_report(self._role)

    def request_exit(self) -> None:
        self._exit = True
        request = getattr(self._port, "request_exit", None)
        if request is not None:
            request()

    async def clear_conversation(self) -> int:
        """Clear the active agent's conversation — history + rendered transcript.

        Drops the stored message history on the Role's ContextManager (so the
        next turn starts fresh) and emits a ``TranscriptCleared`` ViewEvent so
        every consumer wipes what it has shown. The history clear also fires a
        ``HistoryEditedEvent(reason="clear")`` on Telemetry (awaited) so the
        turn-context frontiers + resource side-store re-derive against the emptied
        history. Returns the number of messages cleared (for the handler's notice).
        """
        cleared = await backend.clear_messages(self._role)
        self._projector.deliver_sync(TranscriptCleared())
        return cleared

    async def delete_react_units(self, anchor_ids) -> int:
        """Delete the react-units anchored at ``anchor_ids`` on the active agent.

        Prunes the live context after committing a ``HistoryEditedEvent`` so the
        deleted turns stay gone from both durable projections across
        restart/resume. Returns the number of messages removed. The caller (the
        Textual host) owns the surgical widget removal — no view event is emitted
        here, so no "conversation compacted" boundary marker shows.
        """
        return await backend.delete_react_units(self._role, anchor_ids)

    def list_checkpoints(self) -> list:
        """List the active agent's whole-tree checkpoints (``/rewind`` targets).

        Returns ``[CheckpointEntry, ...]`` from the session rollout — each a
        captured user-turn tree snapshot. Empty when the feature is inert (the
        workspace is not a git repo) or nothing has been captured yet. Safe to
        call between turns (no turn lock held while a command runs).
        """
        return backend.list_checkpoints(self._role)

    async def rewind_to(self, index: int) -> Optional[Any]:
        """Roll the working tree back to checkpoint ``index`` (auto-saving first).

        Delegates to the backend, which snapshots the current tree ("before
        rewind" — so the rewind is itself reversible) then restores the target.
        Returns a :class:`~mote.product.cli.backend.RewindResult` (target entry + any
        externally-modified paths the rewind overwrote) on success, ``None`` on a
        bad index or restore failure.
        """
        return await backend.rewind_files(self._role, index)

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
        self,
        *,
        name: str = "Assistant",
        session_id: Optional[str] = None,
        agent_type: Optional[str] = None,
    ):
        if self._role_factory is None:
            return None
        return self._role_factory(name=name, session_id=session_id, agent_type=agent_type)

    def adopt_role(self, role: Any, *, switch: bool = True, root: bool = False) -> str:
        """Wrap *role* into a runtime, add it to the plane, and wire the console."""
        backend.bind_human_channel(role, PortHumanChannel(self._port))
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
        if self._agent_catalog is None:
            return []
        return backend.list_agent_types(self._agent_catalog)

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

    async def fork_current(self) -> Optional[str]:
        """Fork the current role's session into an independent sibling agent."""
        forked = await backend.fork_role(self._role)
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
