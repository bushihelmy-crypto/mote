"""TokenPressureContextSource — warn when the context window is filling up.

A proactive "secretary" feed: once the stored history crosses the warning
threshold (CC ``calculateTokenWarningState``), nudge the model to write down
anything it needs before older tool results get compacted away. Silent below the
threshold so it costs nothing on a fresh conversation.

Duck-typed: holds any object exposing ``token_state()`` (the ``ContextManager``).
The bus lives in the same ``context`` layer, but reading through the method (not
importing the class) keeps the source trivially fakeable in tests.
"""

from __future__ import annotations

from typing import Optional


class TokenPressureContextSource:
    """Emits a context-pressure reminder when near the compaction threshold."""

    name = "token"
    priority = 20

    def __init__(self, provider) -> None:
        # `provider` is anything with a `token_state()` -> TokenState method.
        self._provider = provider

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        provider = self._provider
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
