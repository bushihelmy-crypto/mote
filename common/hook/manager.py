"""HookManager — selects, runs, and folds hook handlers for one event.

Supports **both** handler forms:
  * **Python callbacks** registered programmatically via :meth:`register` (the
    SDK-style, zero-serialization path) — sync or async callables.
  * **External commands** declared in a ``HookConfig`` (the config-driven,
    CC/codex JSON stdin/stdout contract path).

:meth:`fire` builds the :class:`HookInput`, selects the handlers whose matcher
matches the event's match field (``_matches`` is a port of CC ``matchesPattern``),
runs them all (callbacks in-process; commands via the command handler), and
:func:`fold`\\s the results with deny > ask > allow precedence. It **never
raises**: every handler is wrapped, and a failure is logged and skipped. When no
handler matches, an ``EMPTY`` outcome is returned via a fast path.

Opt-in: when a Role provides neither a ``HookConfig`` nor any registered
callback, the manager is never built (``Role.hook_manager`` is ``None``) and all
call sites short-circuit — identical legacy behavior, zero overhead.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Awaitable, Callable, Optional, Union

from metagpt.common.hook.command_handler import run_command_handler
from metagpt.common.hook.parser import parse_callback_result
from metagpt.common.hook.types import EMPTY, HookInput, HookOutcome, fold
from metagpt.common.logs import log_class, logger

# A hook callback: receives the HookInput, returns None / dict / HookOutcome,
# either synchronously or as a coroutine.
HookCallback = Callable[[HookInput], Union[None, dict, HookOutcome, Awaitable[Any]]]

# Per-event payload key used as the matcher's query. Events absent from this map
# have no match field and so always match (CC semantics).
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
        config: Any = None,
        *,
        session_id: str = "",
        get_cwd: Optional[Callable[[], str]] = None,
        transcript_path: str = "",
    ):
        # ``config`` is an optional ``HookConfig`` (duck-typed: ``.events`` is a
        # dict[event_name -> list[HookMatcherGroup]]). None => command handlers
        # disabled, callbacks only.
        self._config = config
        self._session_id = session_id
        self._get_cwd = get_cwd
        self._transcript_path = transcript_path
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
    # Matching (port of CC ``matchesPattern``)
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

    def _build_input(self, event: str, payload: dict, permission_mode: Optional[str]) -> HookInput:
        cwd = ""
        if self._get_cwd is not None:
            try:
                cwd = self._get_cwd() or ""
            except Exception:  # noqa: BLE001 — cwd accessor must never break a fire
                cwd = ""
        return HookInput(
            hook_event_name=event,
            session_id=self._session_id,
            cwd=cwd,
            transcript_path=self._transcript_path,
            permission_mode=permission_mode,
            payload=payload,
        )

    def _command_handlers(self, event: str, query: Optional[str]) -> list[Any]:
        """The command handlers whose matcher group matches ``query``."""
        events = getattr(self._config, "events", None)
        if not events:
            return []
        groups = events.get(event) or []
        selected: list[Any] = []
        for group in groups:
            matcher = getattr(group, "matcher", None)
            if self._matches(matcher, query):
                selected.extend(getattr(group, "handlers", []) or [])
        return selected

    def _selected_callbacks(self, event: str, query: Optional[str]) -> list[HookCallback]:
        return [fn for (matcher, fn) in self._callbacks.get(event, []) if self._matches(matcher, query)]

    async def _run_callback(self, fn: HookCallback, hook_input: HookInput) -> HookOutcome:
        try:
            result = fn(hook_input)
            if inspect.isawaitable(result):
                result = await result
            return parse_callback_result(result)
        except Exception as exc:  # noqa: BLE001 — one bad hook must not break fire
            logger.warning(f"hook: callback for {hook_input.hook_event_name} raised: {exc}")
            return EMPTY

    async def _run_command(self, cfg: Any, hook_input: HookInput) -> HookOutcome:
        try:
            return await run_command_handler(cfg, hook_input)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"hook: command handler for {hook_input.hook_event_name} raised: {exc}")
            return EMPTY

    async def fire(self, event: str, payload: dict, *, permission_mode: Optional[str] = None) -> HookOutcome:
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
            outcomes.append(await self._run_callback(fn, hook_input))
        for cfg in commands:
            outcomes.append(await self._run_command(cfg, hook_input))
        return fold(outcomes)


__all__ = ["HookManager", "HookCallback"]
