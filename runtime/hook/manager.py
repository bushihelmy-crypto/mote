"""HookManager — selects, runs, and folds hook handlers for one event.

Supports **both** handler forms:
  * **Python callbacks** registered programmatically via :meth:`register` (the
    SDK-style, zero-serialization path) — sync or async callables.
  * **External commands** declared in a ``HookConfig`` (the config-driven,
    JSON stdin/stdout contract path).

:meth:`fire` builds the :class:`HookInput`, selects the handlers whose matcher
matches the event's match field (via ``_matches``), runs them all (callbacks
in-process; commands via the command handler), and
:func:`fold`\\s the results with deny > ask > allow precedence. It **never
raises**: every handler is wrapped, and a failure is logged and skipped. When no
handler matches, an ``EMPTY`` outcome is returned via a fast path.

Opt-in: when a Role provides neither a ``HookConfig`` nor any registered
callback, the manager is never built (``Role.hook_manager`` is ``None``) and all
call sites short-circuit with zero overhead.
"""

from __future__ import annotations

import inspect
import re
import shlex
from typing import Any, Awaitable, Callable, Optional, Union

from mote.contracts.authorization import PermissionMode
from mote.contracts.hook import (
    CompactPayload,
    FileChangedInvocation,
    FileChangedPayload,
    HookAuthorizationFact,
    HookIdentity,
    HookInvocation,
    PostCompactInvocation,
    PostToolUseInvocation,
    PostToolUsePayload,
    PreCompactInvocation,
    PreToolUseInvocation,
    PreToolUsePayload,
    SessionStartInvocation,
    SessionStartPayload,
    StopInvocation,
    StopPayload,
    UserPromptSubmitInvocation,
    UserPromptSubmitPayload,
)
from mote.runtime.config.hook import HookCommandHandler, HookConfig
from mote.runtime.hook.command_handler import HookCommandSandbox, run_command_handler
from mote.runtime.hook.parser import parse_callback_result
from mote.runtime.hook.types import EMPTY, HookOutcome, fold
from mote.runtime.telemetry.logging import log_class, logger
from mote.runtime.tools.permission.engine import PermissionEngine

# A hook callback: receives the HookInput, returns None / dict / HookOutcome,
# either synchronously or as a coroutine.
HookCallback = Callable[[HookInvocation], Union[None, dict, HookOutcome, Awaitable[Any]]]

_CONTROL_EVENTS = frozenset({"PreToolUse"})


def _failure_outcome(event: str) -> HookOutcome:
    if event in _CONTROL_EVENTS:
        return HookOutcome(
            behavior="deny",
            system_message="control hook failed closed",
        )
    return EMPTY


# Per-event payload key used as the matcher's query. Events absent from this map
# have no match field and so always match.
_MATCH_FIELD: dict[str, str] = {
    "PreToolUse": "tool_name",
    "PostToolUse": "tool_name",
    "PreCompact": "trigger",
    "PostCompact": "trigger",
    "SessionStart": "source",
    "FileChanged": "path",
}


@log_class(level="DEBUG", exclude={"enabled", "_matches"})
class HookManager:
    """Holds command-handler config + registered callbacks; fires events."""

    def __init__(
        self,
        config: HookConfig | None = None,
        *,
        session_id: str = "",
        get_cwd: Optional[Callable[[], str]] = None,
        transcript_path: str = "",
        command_sandbox: HookCommandSandbox | None = None,
        permission_engine: PermissionEngine | None = None,
    ):
        # ``config`` is an optional ``HookConfig`` (duck-typed: ``.events`` is a
        # dict[event_name -> list[HookMatcherGroup]]). None => command handlers
        # disabled, callbacks only.
        self._config = config
        self._session_id = session_id
        self._get_cwd = get_cwd
        self._transcript_path = transcript_path
        self._command_sandbox = command_sandbox
        self._permission_engine = permission_engine
        # event_name -> list[(matcher, callback)]
        self._callbacks: dict[str, list[tuple[Optional[str], HookCallback]]] = {}

    # ------------------------------------------------------------------
    # Registration (the programmatic / SDK path)
    # ------------------------------------------------------------------

    def register(self, event: str, fn: HookCallback, matcher: Optional[str] = None) -> None:
        """Register an in-process Python callback for ``event``.

        ``matcher`` follows the same syntax as a command-handler matcher
        (``None``/``*`` = all, ``A|B`` = exact pipe list, else regex).
        """
        self._callbacks.setdefault(event, []).append((matcher, fn))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(matcher: Optional[str], query: Optional[str]) -> bool:
        """Return True when ``matcher`` matches ``query``.

        * empty / ``None`` / ``*`` => matches everything;
        * ``A|B|C`` => exact membership in the pipe-separated list;
        * otherwise => treated as a regex (full-string search).

        Events without a match field pass ``query=None`` and always match.
        """
        if matcher is None or matcher == "" or matcher == "*":
            return True
        if query is None:
            # No field to match against -> the event always fires its handlers.
            return True
        if "|" in matcher:
            return query in matcher.split("|")
        try:
            return re.search(matcher, query) is not None
        except re.error:
            # A malformed regex matcher degrades to an exact compare.
            return matcher == query

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Cheap short-circuit: any command groups or registered callbacks."""
        if self._callbacks:
            return True
        events = getattr(self._config, "events", None)
        return bool(events)

    def _build_input(
        self,
        event: str,
        payload: dict,
        permission_mode: PermissionMode | None,
    ) -> HookInvocation:
        identity = self._identity()
        factories = {
            "PreToolUse": lambda: PreToolUseInvocation(identity, permission_mode, PreToolUsePayload(**payload)),
            "PostToolUse": lambda: PostToolUseInvocation(identity, PostToolUsePayload(**payload)),
            "UserPromptSubmit": lambda: UserPromptSubmitInvocation(identity, UserPromptSubmitPayload(**payload)),
            "SessionStart": lambda: SessionStartInvocation(identity, SessionStartPayload(**payload)),
            "Stop": lambda: StopInvocation(identity, StopPayload(**payload)),
            "PreCompact": lambda: PreCompactInvocation(identity, CompactPayload(**payload)),
            "PostCompact": lambda: PostCompactInvocation(identity, CompactPayload(**payload)),
        }
        try:
            return factories[event]()
        except KeyError as exc:
            raise ValueError(f"unsupported hook event: {event}") from exc

    def _command_handlers(self, event: str, query: Optional[str]) -> list[HookCommandHandler]:
        """The command handlers whose matcher group matches ``query``."""
        events = getattr(self._config, "events", None)
        if not events:
            return []
        groups = events.get(event) or []
        selected: list[HookCommandHandler] = []
        for group in groups:
            matcher = getattr(group, "matcher", None)
            if self._matches(matcher, query):
                selected.extend(getattr(group, "handlers", []) or [])
        return selected

    def _selected_callbacks(self, event: str, query: Optional[str]) -> list[HookCallback]:
        return [fn for (matcher, fn) in self._callbacks.get(event, []) if self._matches(matcher, query)]

    async def _run_callback(self, event: str, fn: HookCallback, hook_input: HookInvocation) -> HookOutcome:
        try:
            result = fn(hook_input)
            if inspect.isawaitable(result):
                result = await result
            return parse_callback_result(result)
        except Exception as exc:  # noqa: BLE001 — one bad hook must not break fire
            logger.warning("hook callback failed")
            return _failure_outcome(event)

    async def _run_command(
        self,
        event: str,
        cfg: HookCommandHandler,
        hook_input: HookInvocation,
    ) -> HookOutcome:
        try:
            engine = self._permission_engine
            if engine is None:
                outcome = _failure_outcome(event)
                outcome.authorization_facts.append(HookAuthorizationFact(cfg.id, "deny"))
                return outcome
            target = shlex.join(cfg.argv)
            decision = await engine.check(
                "HookCommand",
                target=target,
                segments=[target],
            )
            if decision.behavior != "allow":
                outcome = _failure_outcome(event)
                outcome.authorization_facts.append(HookAuthorizationFact(cfg.id, "deny"))
                return outcome
            outcome = await run_command_handler(
                cfg,
                hook_input,
                sandbox=self._command_sandbox,
            )
            outcome.authorization_facts.append(HookAuthorizationFact(cfg.id, "allow"))
            return outcome
        except Exception as exc:  # noqa: BLE001
            logger.warning("hook command handler failed")
            return _failure_outcome(event)

    async def fire(
        self,
        event: str,
        payload: dict,
        *,
        permission_mode: PermissionMode | None = None,
    ) -> HookOutcome:
        """Fire ``event``: select matching handlers, run them, fold the results.

        Never raises. Returns ``EMPTY`` immediately when no handler matches.
        """
        query = payload.get(_MATCH_FIELD[event]) if event in _MATCH_FIELD else None

        callbacks = self._selected_callbacks(event, query)
        commands = self._command_handlers(event, query)
        if not callbacks and not commands:
            return EMPTY

        hook_input = self._build_input(event, payload, permission_mode)

        outcomes: list[HookOutcome] = []
        for fn in callbacks:
            outcomes.append(await self._run_callback(event, fn, hook_input))
        for cfg in commands:
            outcomes.append(await self._run_command(event, cfg, hook_input))
        return fold(outcomes)

    async def fire_file_changed(self, payload: FileChangedPayload) -> HookOutcome:
        """Fire one canonical file transition without a mapping reconstruction."""
        callbacks = self._selected_callbacks("FileChanged", payload.path)
        commands = self._command_handlers("FileChanged", payload.path)
        if not callbacks and not commands:
            return EMPTY
        identity = self._identity()
        hook_input = FileChangedInvocation(identity, payload)
        outcomes: list[HookOutcome] = []
        for fn in callbacks:
            outcomes.append(await self._run_callback("FileChanged", fn, hook_input))
        for cfg in commands:
            outcomes.append(await self._run_command("FileChanged", cfg, hook_input))
        return fold(outcomes)

    def _identity(self) -> HookIdentity:
        cwd = ""
        if self._get_cwd is not None:
            try:
                cwd = self._get_cwd() or ""
            except Exception:  # noqa: BLE001
                cwd = ""
        return HookIdentity(self._session_id, cwd, self._transcript_path)


__all__ = ["HookManager", "HookCallback"]
