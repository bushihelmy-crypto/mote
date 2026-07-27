"""TokenPressureContextSource — warn when the context window is filling up.

A proactive "secretary" feed: once the stored history crosses the warning
threshold, nudge the model to write down
anything it needs before older tool results get compacted away. Silent below the
threshold so it costs nothing on a fresh conversation.

Duck-typed: holds any object exposing ``token_state()`` (the ``ContextManager``).
The bus lives in the same ``context`` layer, but reading through the method (not
importing the class) keeps the source trivially fakeable in tests.

The provider may be supplied either directly (an object with ``token_state()``,
or ``None``) or as a zero-arg getter callable resolved lazily on each render —
the latter lets the Role inject ``lambda: self.context_manager`` without forcing
that collaborator to be built at roster-assembly time (which would knot the
Telemetry ↔ context-manager construction cycle).
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, Union

from mote.contracts.ports import TurnContextPriority


class _TokenStateProvider(Protocol):
    """The context-manager slice this source reads (duck-typed).

    Structural only — reading through ``token_state()`` (not importing the
    ``ContextManager``) keeps the source trivially fakeable in tests.
    """

    def token_state(self) -> object:
        ...


class TokenPressureContextSource:
    """Emits a context-pressure reminder when near the compaction threshold."""

    name = "token"
    priority = TurnContextPriority.TOKEN
    # Ephemeral (request-only): a "context filling up" nudge is a transient state
    # signal, only meaningful on the turn it fires. Re-evaluated every cycle from
    # the live token state, so persisting it would just leave stale warnings in
    # history (and inflate the very budget it warns about).
    save_to_context = False

    def __init__(
        self,
        provider: Union[
            _TokenStateProvider,
            Callable[[], Optional[_TokenStateProvider]],
            None,
        ],
    ) -> None:
        # `provider` is anything with a `token_state()` -> TokenState method, or a
        # zero-arg callable returning one (resolved lazily per render), or None.
        self._provider = provider

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        provider = self._provider
        # A getter callable (e.g. ``lambda: self.context_manager``) is resolved
        # on demand; a bare provider object has no ``__call__`` so passes through.
        if callable(provider):
            provider = provider()
        if provider is None:
            return None
        state = provider.token_state()
        if state is None or not getattr(state, "above_warning", False):
            return None
        percent = getattr(state, "percent_left", 0)
        return (
            "# Context budget\n"
            f"The context window is filling up (~{percent}% headroom left before "
            "automatic compaction). Older tool results may be cleared soon — write "
            "down any details you will need later directly in your response now."
        )


__all__ = ["TokenPressureContextSource"]
