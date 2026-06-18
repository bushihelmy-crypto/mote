#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The interactive REPL: an unbounded conversation loop over ``AgentControl``.

Each non-empty input line triggers exactly one turn (one ``Role.run()``, which
may itself loop ReAct internally) through the control plane's persistent driver.
The loop never terminates on its own — only an explicit exit does:

  * Ctrl+C **during a turn**  → interrupt the in-flight turn, return to prompt.
  * Ctrl+C **at the prompt**  → arm a double-press window; a second Ctrl+C
    within the window exits, otherwise it just re-prompts.
  * Ctrl+D (EOF) at the prompt → exit.

The two-stage Ctrl+C is a timing/state-machine (not a signal counter), matching
codex / claude-code. Because the persistent driver discards ``run_one_turn()``'s
return value, the REPL reads the reply back from ``role.state.context.messages``
after each turn.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Any, Callable, Optional

from metagpt.common.logs import logger
from metagpt.common.schema import UserMessage
from metagpt.cli.render import build_renderer
from metagpt.common.config.meta_config import Config
from metagpt.common.utils.git_state import find_git_root
from metagpt.environment.control import AgentControl
from metagpt.environment.runtime import AgentRuntime
from metagpt.roles import Role
from metagpt.roles.role_schema import RoleSchema
from metagpt.roles.role_state import RoleState
from metagpt.router.llm.context import Context
from metagpt.cli.commands import SlashCommands
from metagpt.environment.runtime import AgentRuntime
from metagpt.common.logs import suspend_console_log
from metagpt.common.logs import resume_console_log


class _RenderSubscriber:
    """Unified bus subscriber handling all rendering events for the REPL.

    Consolidates what used to be two separate mechanisms:
      1. ``_StreamRenderSubscriber`` — for streamed LLM tokens (sync path)
      2. Hook callbacks via ``register_hook("PreToolUse"/"PostToolUse")`` — for
         tool-call panels

    Plus a new concern:
      3. ``TaskProgressEvent`` — bggraph node progress display

    Stream deltas and task progress are emitted synchronously
    (``emit_event_sync``) and delivered via ``handle_sync``. Tool-use events are
    control events dispatched via the async ``handle`` path (they carry
    outcomes), though this subscriber is read-only (returns ``None``).

    One instance is shared across every role's bus — each role owns its own
    :class:`~metagpt.common.events.EventBus`, so subscribing per role never
    double-delivers.
    """

    priority = 50

    def __init__(self, repl: "Repl"):
        self._repl = repl

    def handle_sync(self, event) -> None:
        from metagpt.common.events import LLMStreamDeltaEvent, TaskProgressEvent

        if isinstance(event, LLMStreamDeltaEvent):
            self._repl._stream_sink(event.token)
        elif isinstance(event, TaskProgressEvent):
            self._repl._on_task_progress(event)

    async def handle(self, event):
        from metagpt.common.events import PreToolUseEvent, PostToolUseEvent

        if isinstance(event, PreToolUseEvent):
            self._repl._on_pre_tool(event)
        elif isinstance(event, PostToolUseEvent):
            self._repl._on_post_tool(event)
        return None


def _format_turn_error(err: BaseException) -> str:
    """Render a turn's exception into a concise one/two-line message for the REPL.

    Typed :class:`MetaGPTError` subclasses (e.g. ``LLMServerError``) carry a clean
    ``message`` plus an optional upstream ``status_code``; anything else falls
    back to ``Type: str(err)``.
    """
    cls = type(err).__name__
    detail = str(err).strip() or repr(err)
    status = getattr(err, "status_code", None)
    if status is not None:
        return f"{cls} (HTTP {status}): {detail}"
    return f"{cls}: {detail}"


class _ConsoleHumanChannel:
    """Minimal env adapter routing the agent's ``ask_human`` to the REPL console.

    ``Role.ask_human`` (the capability behind the ``AskUserQuestion`` tool)
    delegates to ``state.env.ask_human(...)``. The REPL drives a single agent and
    reads replies straight from the role's context history, so only the
    human-input channel is wired; address registration / message publishing the
    Role might call on its env are inert no-ops.
    """

    # The provider reads ``env.desc`` / ``env.role_names()`` / ``env.roles`` when
    # building the role prefix + team info. The single-agent REPL has no
    # multi-role environment, so all three are inert: an empty desc/roles
    # short-circuits the "other roles" and "team info" prefixes entirely.
    desc: str = ""
    roles: dict = {}

    def __init__(self, ask: Callable[[str], "Any"]):
        self._ask = ask  # async (question: str) -> str

    def role_names(self) -> list:
        return []

    async def ask_human(self, question: str, sent_from: Any = None) -> str:
        return await self._ask(question)

    async def reply_to_human(self, content: str, sent_from: Any = None) -> str:
        return ""

    def set_addresses(self, role: Any, addresses: Any) -> None:  # noqa: D401 — no-op
        pass

    def publish_message(self, msg: Any) -> None:  # noqa: D401 — no-op
        pass


class Repl:
    """An interactive read-eval-print loop driving one agent via ``AgentControl``."""

    def __init__(
        self,
        control: Any,
        agent_id: str,
        role: Any,
        *,
        prompt: str = "\u203a ",
        double_press_window: float = 2.0,
        out=None,
        get_input_reader: Optional[Callable[[], Any]] = None,
        renderer: Any = None,
        role_factory: Optional[Callable[..., Any]] = None,
    ):
        self._control = control
        self._agent_id = agent_id
        self._role = role
        self._prompt = prompt
        self._double_press_window = double_press_window
        self._out = out if out is not None else sys.stdout
        self._get_input_reader = get_input_reader
        # Optional rich renderer (tool-call panels, colored streaming). When
        # ``None`` the loop keeps its plain-text output path unchanged.
        self._renderer = renderer
        # Unified bus subscriber: handles stream deltas, tool-call panels, and
        # task progress. Subscribed per role in ``adopt_role`` / ``run``,
        # unsubscribed in ``_teardown``.
        self._render_sub = _RenderSubscriber(self)
        self._render_buses: list = []  # buses we subscribed to, for teardown
        self._console_log_suspended = False  # stderr log sink muted while REPL owns stdout
        # Builds fresh / resumed roles (sharing config + context); injected by
        # ``build_repl``. ``None`` => /new and /resume are unavailable.
        self._role_factory = role_factory


        self._commands = SlashCommands(self)
        self._last_sessions: list = []  # cached for index-based /resume

        self._running_turn = False
        self._should_exit = False
        # Set whenever LLM tokens streamed live to the console during the current
        # turn (see ``_stream_sink``); used to avoid reprinting the same text.
        self._streamed_this_turn = False
        self._reader: Any = None
        self._read_task: Optional[asyncio.Task] = None
        # When a background-task-triggered AskUserQuestion runs while the main
        # loop is parked on a pending stdin read, the question's answer must be
        # routed to the waiting tool instead of being consumed as new turn
        # input. ``_console_ask`` parks this future; ``_read_line_or_idle_reply``
        # fulfills it with the next line (single stdin reader — see _console_ask).
        self._ask_waiter: Optional[asyncio.Future] = None
        self._last_sigint_ts: Optional[float] = None
        # Baseline message count captured when the loop parks at the prompt, used
        # to detect replies produced by background-task-triggered turns that run
        # while the REPL is idle (see ``_drain_idle_replies``). The interval also
        # bounds how often the idle poll wakes to check for them.
        self._idle_baseline = 0
        self._idle_poll_interval = 0.1
        # The input that triggered the in-flight turn (so an interrupt can
        # restore it) and the value staged for restore at the next prompt.
        self._current_input: Optional[str] = None
        self._restored_input: Optional[str] = None

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------
    def _write(self, text: str) -> None:
        if self._renderer is not None:
            try:
                self._renderer.write(text)
                return
            except Exception:  # noqa: BLE001 — fall back to plain stdout
                pass
        try:
            self._out.write(text)
            self._out.flush()
        except Exception:  # noqa: BLE001 — never let console I/O crash the loop
            pass

    def _notice(self, text: str) -> None:
        """System notices (^C / interrupt / restore hints)."""
        if self._renderer is not None:
            try:
                self._renderer.notice(text)
                return
            except Exception:  # noqa: BLE001
                pass
        self._write(text)

    def _error(self, text: str) -> None:
        """A failed turn surfaced to the user (renderer panel or plain text)."""
        if self._renderer is not None:
            try:
                self._renderer.error(text)
                return
            except Exception:  # noqa: BLE001
                pass
        self._write(f"\n[error] {text}\n")

    def _reprompt(self) -> None:
        if self._renderer is not None:
            try:
                self._renderer.prompt(self._prompt)
                return
            except Exception:  # noqa: BLE001
                pass
        self._write(self._prompt)

    # ------------------------------------------------------------------
    # stdin setup (cancellable, event-loop integrated)
    # ------------------------------------------------------------------
    async def _setup_stdin(self) -> None:
        if self._get_input_reader is not None:
            self._reader = self._get_input_reader()
            return
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        self._reader = reader

    # ------------------------------------------------------------------
    # SIGINT handling (two-stage state machine, aligned with cc)
    # ------------------------------------------------------------------
    def _on_sigint(self) -> None:
        if self._running_turn:
            # Mid-turn: abort the in-flight turn and return to the prompt,
            # staging the turn's prompt for restore at the next prompt.
            self._notice("\n^C  interrupting current turn\u2026\n")
            self._last_sigint_ts = None
            if self._current_input:
                self._restored_input = self._current_input
            asyncio.ensure_future(self._control.interrupt(self._agent_id))
        else:
            # Idle prompt: double-press within the window to exit.
            now = time.monotonic()
            if self._last_sigint_ts is not None and now - self._last_sigint_ts <= self._double_press_window:
                self._should_exit = True
                if self._read_task is not None:
                    self._read_task.cancel()
            else:
                self._last_sigint_ts = now
                self._notice("\n(Press Ctrl-C again to exit)\n")
                self._reprompt()

    # ------------------------------------------------------------------
    # Read a line (cancellable by SIGINT)
    # ------------------------------------------------------------------
    async def _read_line(self) -> Optional[str]:
        # An interrupted turn stages its prompt here. A line-buffered tty cannot
        # be pre-filled with editable text (that needs raw/readline mode), so we
        # show it and let Enter resend it verbatim or new text replace it.
        restored = self._restored_input
        self._restored_input = None
        if restored:
            self._notice(
                "(interrupted — press Enter to resend, or type a new message)\n"
                f"  {restored}\n"
            )
        self._reprompt()
        # Capture the baseline so a background-task-triggered turn that runs while
        # we're parked here can be detected and its reply printed (see
        # ``_drain_idle_replies``), instead of staying invisible until the user
        # types the next line. Reset the stream flag so tokens streamed by such a
        # turn flip it, letting the drain avoid reprinting an already-shown reply.
        self._idle_baseline = len(self._role.state.context.messages)
        self._streamed_this_turn = False
        self._read_task = asyncio.ensure_future(self._reader.readline())
        try:
            data = await self._read_line_or_idle_reply()
        except asyncio.CancelledError:
            return None  # idle exit (double Ctrl+C)
        finally:
            self._read_task = None
        if not data:  # EOF (Ctrl+D)
            self._should_exit = True
            return None
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        line = data.rstrip("\n")
        if restored and not line.strip():
            return restored  # bare Enter resends the interrupted prompt
        return line

    async def _read_line_or_idle_reply(self) -> Any:
        """Await stdin while polling for background-task-triggered replies.

        The persistent driver may run a fresh turn (woken by a completed
        background task) while the loop is parked at the prompt. Such a turn's
        reply lands in ``role.state.context.messages`` but would never be printed
        until the next manual input. We poll alongside the stdin read and flush
        those replies as they appear, re-prompting afterwards so the prompt stays
        at the bottom. Returns the raw stdin data once the user submits a line.

        This is the SOLE reader of ``self._reader`` while the loop is parked. If
        a background-task turn parks ``_console_ask`` (an AskUserQuestion needing
        a typed answer) during the wait, the next line is routed to that waiter
        rather than returned as new turn input — otherwise two concurrent
        ``readline()`` calls would race for the same line and the question's
        answer could be silently stolen by the main loop.
        """
        while True:
            done, _ = await asyncio.wait(
                {self._read_task}, timeout=self._idle_poll_interval
            )
            if self._read_task in done:
                data = self._read_task.result()
                waiter = self._ask_waiter
                if waiter is not None and not waiter.done():
                    # An AskUserQuestion is parked: hand it this line and keep
                    # reading for the loop's own next input.
                    waiter.set_result(data)
                    self._ask_waiter = None
                    self._read_task = asyncio.ensure_future(self._reader.readline())
                    continue
                return data
            # stdin still pending — surface any reply produced while idle.
            self._drain_idle_replies()

    def _drain_idle_replies(self) -> None:
        """Print assistant replies appended since the prompt was last shown.

        Only fires when not mid-turn and new messages exist beyond the idle
        baseline (a background-task-triggered turn). Advances the baseline and
        re-prompts so subsequent idle replies are printed exactly once.
        """
        if self._running_turn:
            return
        messages = self._role.state.context.messages
        if len(messages) <= self._idle_baseline:
            return
        self._finish_stream()
        # When the idle turn streamed its reply live, the tokens already reached
        # the console (plain stdout or the renderer's Live region), so reprinting
        # from context would duplicate it — just advance the baseline + reprompt.
        if self._streamed_this_turn:
            self._streamed_this_turn = False
            self._idle_baseline = len(messages)
            self._reprompt()
            return
        printed = self._print_assistant_since(self._idle_baseline)
        self._idle_baseline = len(messages)
        if printed:
            self._reprompt()

    # ------------------------------------------------------------------
    # Console ask channel (AskUserQuestion -> stdin)
    # ------------------------------------------------------------------
    async def _console_ask(self, question: str) -> str:
        """Print a question and read one answer line from stdin.

        Invoked from inside a running turn by the ``AskUserQuestion`` tool. Two
        cases, both of which keep a SINGLE reader on ``self._reader``:

        * Foreground turn: the main loop is blocked in ``_run_turn`` with no
          pending stdin read, so this method owns the reader and reads directly.
        * Background-task turn while idle: the main loop is parked in
          ``_read_line_or_idle_reply`` with a pending ``readline()``. Starting a
          second ``readline()`` here would race that one for the same line and
          the answer could be stolen as new turn input. Instead we park
          ``_ask_waiter``; the main loop routes the next line to it.
        """
        self._write(f"\n{question}\n")
        self._reprompt()
        if self._reader is None:
            return ""
        if self._read_task is not None and not self._read_task.done():
            # Main loop is already reading stdin — route the answer through it.
            waiter: asyncio.Future = asyncio.get_event_loop().create_future()
            self._ask_waiter = waiter
            try:
                data = await waiter
            finally:
                if self._ask_waiter is waiter:
                    self._ask_waiter = None
        else:
            data = await self._reader.readline()
        if not data:
            return ""
        if isinstance(data, bytes):
            data = data.decode(errors="replace")
        return data.rstrip("\n")

    # ------------------------------------------------------------------
    # Run one turn through the control plane
    # ------------------------------------------------------------------
    async def _run_turn(self, text: str) -> None:
        before = len(self._role.state.context.messages)
        self._running_turn = True
        self._streamed_this_turn = False  # reset; the stream sink flips it if tokens arrive
        self._current_input = text  # staged for restore if this turn is interrupted
        self._last_sigint_ts = None  # entering a turn clears the double-press timer
        try:
            self._control.send_input(self._agent_id, UserMessage(content=text))
            await asyncio.sleep(0)  # let the driver pick up the wake / set active_turn
            while not self._control.quiescent():
                await asyncio.sleep(0.05)
        finally:
            self._running_turn = False
            self._current_input = None
        # Close any live streaming region opened during the turn (renderer mode).
        # Must happen before the print/error path: when the reply streamed we
        # skip reprinting it, so nothing else would finalize the Live region.
        self._finish_stream()
        # A turn that ended in ERRORED leaves no assistant reply, so the user
        # would otherwise see nothing. Surface the failure (e.g. protocol
        # mismatch, LLM 5xx) instead of a blank prompt.
        runtime = self._control.get_runtime(self._agent_id)
        err = getattr(runtime, "last_error", None) if runtime is not None else None
        if err is not None:
            self._error(_format_turn_error(err))
        self._print_new_assistant_messages(before)

    def _print_new_assistant_messages(self, before: int) -> None:
        """Print assistant replies appended since *before*.

        When the reply already streamed live we skip reprinting it: in plain mode
        the tokens went straight to stdout, and in renderer mode the live Markdown
        region already rendered the final reply (see ConsoleRenderer.stream /
        end_stream). Reprinting either way would duplicate the text. For a
        non-streaming provider the text only lands in the context here, so it is
        always printed.
        """
        if self._streamed_this_turn:
            return
        self._print_assistant_since(before)

    def _print_assistant_since(self, before: int) -> bool:
        """Print assistant replies appended since *before*; return True if any.

        Unconditional printer (no streaming guard) shared by the post-turn print
        and the idle-reply drain. Routes through the renderer when present, else
        plain stdout.
        """
        printed = False
        messages = self._role.state.context.messages
        for msg in messages[before:]:
            if getattr(msg, "role", None) != "assistant":
                continue
            content = getattr(msg, "content", "") or ""
            if not content.strip():
                continue
            printed = True
            if self._renderer is not None:
                try:
                    self._renderer.assistant(content)
                    continue
                except Exception:  # noqa: BLE001 — fall back to plain text
                    pass
            self._write(f"\n{content}\n")
        return printed

    # ------------------------------------------------------------------
    # Multi-agent: membership / switching (driven by slash commands)
    # ------------------------------------------------------------------
    @property
    def current_agent_id(self) -> str:
        return self._agent_id

    def request_exit(self) -> None:
        """Signal the loop to exit and cancel any pending read (from /exit)."""
        self._should_exit = True
        if self._read_task is not None:
            self._read_task.cancel()

    def active_agents(self) -> list:
        """Return ``[(agent_id, name, status), ...]`` for the live control plane."""
        out = []
        for agent_id, runtime in self._control.runtimes().items():
            name = getattr(getattr(runtime.role, "role_schema", None), "name", "?")
            status = self._control.get_status(agent_id).value
            out.append((agent_id, name, status))
        return out

    def _set_active(self, agent_id: str) -> bool:
        runtime = self._control.get_runtime(agent_id)
        if runtime is None:
            return False
        self._agent_id = agent_id
        self._role = runtime.role
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
        """Resolve a reference to a live agent_id: index | session-id(/prefix) | name."""
        agents = list(self._control.runtimes().items())
        hit = self._resolve_ref(ref, [aid for aid, _ in agents])
        if hit is not None:
            return hit
        named = [aid for aid, rt in agents if getattr(getattr(rt.role, "role_schema", None), "name", "") == ref.strip()]
        return named[0] if len(named) == 1 else None

    def switch_agent(self, ref: str) -> Optional[tuple]:
        """Switch the active agent. Returns ``(agent_id, name)`` or ``None``."""
        agent_id = self._resolve_agent(ref)
        if agent_id is None or not self._set_active(agent_id):
            return None
        runtime = self._control.get_runtime(agent_id)
        name = getattr(getattr(runtime.role, "role_schema", None), "name", "?")
        return agent_id, name

    def _make_role(self, *, name: str = "Assistant", session_id: Optional[str] = None):
        if self._role_factory is None:
            return None
        return self._role_factory(name=name, session_id=session_id)

    def adopt_role(self, role: Any, *, switch: bool = True, root: bool = False) -> str:
        """Wrap *role* into a runtime, add it to the plane, wire console + bus."""

        role.state.env = _ConsoleHumanChannel(self._console_ask)
        self._subscribe_render(role)
        runtime = AgentRuntime(role)
        self._control.add_agent(runtime, root=root)
        if switch:
            self._set_active(role.session_id)
        return role.session_id

    def new_agent(self, name: str = "Assistant") -> Optional[str]:
        role = self._make_role(name=name)
        if role is None:
            return None
        return self.adopt_role(role, switch=True)

    def fork_current(self) -> Optional[str]:
        """Fork the current role's session into an independent sibling agent."""
        if not hasattr(self._role, "fork_session"):
            return None
        try:
            forked = self._role.fork_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: fork_session failed: {exc}")
            return None
        return self.adopt_role(forked, switch=True)

    # ------------------------------------------------------------------
    # Session resume / list (driven by slash commands)
    # ------------------------------------------------------------------
    def list_resumable_sessions(self) -> list:
        """List resumable sessions (newest first), caching for index-based resume."""
        try:
            sessions = type(self._role).list_sessions()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: list_sessions failed: {exc}")
            sessions = []
        self._last_sessions = sessions
        return sessions

    def _resolve_session_ref(self, ref: str) -> Optional[str]:
        ref = ref.strip()
        if ref.isdigit():
            # An index maps to the listing the user last saw (via /sessions).
            i = int(ref)
            return self._last_sessions[i].session_id if 0 <= i < len(self._last_sessions) else None
        # id / prefix: resolve against a fresh listing so new sessions are visible.
        hit = self._resolve_ref(ref, [s.session_id for s in self.list_resumable_sessions()])
        if hit is not None:
            return hit
        # Allow a full id not present in the listing.
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
            ok = role.resume_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: resume_session failed: {exc}")
            return False, f"failed to resume {sid[:8]}"
        if not ok:
            return False, f"no rollout for {sid[:8]}"
        self.adopt_role(role, switch=True)
        return True, f"resumed session {sid[:8]}"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await self._setup_stdin()
        try:
            loop.add_signal_handler(signal.SIGINT, self._on_sigint)
        except (NotImplementedError, RuntimeError):
            pass  # platform without signal-handler support — degrade gracefully
        self._suspend_console_log()
        self._subscribe_render(self._role)
        self._control.start()
        try:
            while not self._should_exit:
                text = await self._read_line()
                if self._should_exit or text is None:
                    break
                if not text.strip():
                    continue
                if self._commands.is_command(text):
                    await self._commands.handle(text)
                    continue
                await self._run_turn(text)
        finally:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except Exception:  # noqa: BLE001
                pass
            await self._teardown()

    def _subscribe_render(self, role: Any) -> None:
        """Subscribe the REPL's unified render subscriber to *role*'s event bus.

        Done per role (not once) so switched-to / forked / freshly-created
        agents also mirror their streamed tokens, tool-call panels, and task
        progress live. Each role owns its own bus, so we subscribe the shared
        subscriber once per bus (tracked in ``_render_buses`` for teardown).
        Best-effort — rendering is optional.
        """
        bus = getattr(role, "event_bus", None)
        if bus is None or bus in self._render_buses:
            return
        try:
            bus.subscribe(self._render_sub)
            self._render_buses.append(bus)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: render subscribe failed: {exc}")

    def _unsubscribe_render(self) -> None:
        for bus in self._render_buses:
            try:
                bus.unsubscribe(self._render_sub)
            except Exception:  # noqa: BLE001
                pass
        self._render_buses = []

    # ------------------------------------------------------------------
    # Bus event handlers (called by _RenderSubscriber)
    # ------------------------------------------------------------------
    def _on_pre_tool(self, event: Any) -> None:
        """Render a tool-call panel from a PreToolUseEvent."""
        if self._renderer is None:
            return
        try:
            self._renderer.pre_tool(event)
        except Exception:  # noqa: BLE001
            pass

    def _on_post_tool(self, event: Any) -> None:
        """Render a tool-result line from a PostToolUseEvent."""
        if self._renderer is None:
            return
        try:
            self._renderer.post_tool(event)
        except Exception:  # noqa: BLE001
            pass

    def _on_task_progress(self, event: Any) -> None:
        """Render a bggraph node progress line from a TaskProgressEvent."""
        if self._renderer is None:
            return
        try:
            self._renderer.task_progress(event)
        except Exception:  # noqa: BLE001
            pass

    def _suspend_console_log(self) -> None:
        """Mute the loguru stderr sink so log lines don't interleave with the REPL.

        All levels still reach the dated log file; the stderr sink is restored in
        :meth:`_teardown`. Best-effort.
        """
        try:

            self._console_log_suspended = suspend_console_log()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: suspend_console_log failed: {exc}")

    def _resume_console_log(self) -> None:
        if not self._console_log_suspended:
            return
        try:
            resume_console_log()
        except Exception:  # noqa: BLE001
            pass
        self._console_log_suspended = False

    def _stream_sink(self, token: Any) -> None:
        """Global ``log_llm_stream`` sink: mirror tokens live + flag the turn.

        Routes streamed LLM tokens to the renderer (dim preview) or plain stdout,
        and flips ``_streamed_this_turn`` so the post-turn print can avoid
        duplicating text that already streamed.
        """
        self._streamed_this_turn = True
        text = token if isinstance(token, str) else str(token)
        if self._renderer is not None:
            try:
                self._renderer.stream(text)
                return
            except Exception:  # noqa: BLE001 — fall back to plain stdout
                pass
        try:
            self._out.write(text)
            self._out.flush()
        except Exception:  # noqa: BLE001 — never let console I/O crash the loop
            pass

    def _finish_stream(self) -> None:
        """Finalize the renderer's live stream region (best-effort, no-op if none).

        Plain-text mode has no Live region, so this is only meaningful with a
        renderer; ``getattr`` tolerates renderers (or test doubles) that predate
        ``end_stream``.
        """
        if self._renderer is None:
            return
        end = getattr(self._renderer, "end_stream", None)
        if end is None:
            return
        try:
            end()
        except Exception:  # noqa: BLE001 — finalizing must never crash the loop
            pass

    async def _teardown(self) -> None:
        self._unsubscribe_render()
        self._resume_console_log()
        try:
            await self._control.stop()
        except Exception as exc:  # noqa: BLE001 — best-effort shutdown
            logger.warning(f"Repl: control.stop() failed: {exc}")
        try:
            await self._role.cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Repl: role.cleanup() failed: {exc}")


def build_repl(
    *,
    model: Optional[str] = None,
    tools: Optional[list[str]] = None,
    cwd: Optional[str] = None,
    name: str = "Assistant",
) -> Repl:
    """Assemble Config -> Context -> Role -> AgentRuntime -> AgentControl -> Repl."""


    config = Config.default(**({"llm__model": model} if model else {}))
    context = Context(config=config)

    def role_factory(*, name: str = name, session_id: Optional[str] = None):
        """Build a role sharing this REPL's config + context.

        Used for the initial agent and (via the REPL) for ``/new`` / ``/resume``,
        which pin a given ``session_id``. The three startup directories follow
        the same rule as a one-shot run: ``working_dir`` is the live cwd (follows
        ``cd``), while ``original_working_dir`` / ``project_root`` are immutable
        anchors (the latter walks up to the git root, else falls back to cwd).
        A resumed role overwrites these from its own ``session_meta``.
        """
        schema = RoleSchema(name=name, tools=list(tools)) if tools else RoleSchema(name=name)
        state = RoleState(session_id=session_id) if session_id else RoleState()
        built = Role(name=name, role_schema=schema, state=state, context=context)
        if cwd:
            built.state.working_dir = cwd
            built.state.original_working_dir = cwd
            built.state.project_root = find_git_root(cwd) or cwd
        return built

    role = role_factory(name=name)
    runtime = AgentRuntime(role)
    control = AgentControl(session_id=role.session_id)
    control.add_agent(runtime, root=True)
    repl = Repl(control, role.session_id, role, renderer=build_renderer(), role_factory=role_factory)
    # Route the AskUserQuestion tool's human channel to the REPL console.
    role.state.env = _ConsoleHumanChannel(repl._console_ask)
    return repl


def run_repl(**kwargs) -> None:
    """Build a REPL and run it to completion (blocking)."""
    repl = build_repl(**kwargs)
    asyncio.run(repl.run())


__all__ = ["Repl", "build_repl", "run_repl"]
