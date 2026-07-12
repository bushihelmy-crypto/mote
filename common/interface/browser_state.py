"""BrowserStateStore protocol — the persistent-browser state capture slice.

The narrow face the ``WebBrowser`` tool uses to record the *final browsing
state* of its live Playwright session (the open tabs' URLs + an optional
``storage_state`` carrying cookies / localStorage = the logged-in session)
just after an action settles, without importing the concrete ``session``
implementation.

Why a Protocol here (not in ``session``): the ``executor`` layer must never
import the ``roles`` layer (the strict downward-only layering rule). The concrete
``BrowserStateRecorder`` lives in ``session`` and is *injected* into the tool as
a Role capability (``record_browser_state``); the tool only depends on this
structural face, so no upward import is introduced.

Mirrors :class:`~metagpt.common.interface.TerminalStateStore` and
:class:`~metagpt.common.interface.KernelStateStore`: a leaf module that only
needs ``typing``, importable from anywhere without risking a cycle. Kept a
separate type (rather than reusing the terminal/kernel stores) because the
browser restore is independent — it re-seeds a different runtime (a browser
context) via a different mechanism and must not clobber the others on replay.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class BrowserStateStore(Protocol):
    """Records the final browsing state of a persistent Playwright session.

    Implemented by ``session.BrowserStateRecorder`` (production) and any test
    double. Called by the ``WebBrowser`` tool after an action settles, with the
    captured open-tab URLs + optional storage_state. The store appends a
    browser-state event (last-write-wins) to the rollout. It owns its own
    enable/disable and persistence, and must be cheap and non-throwing from the
    tool's point of view.

    Note: ``storage_state`` may carry cookies / localStorage (the logged-in
    session), so it is written into the rollout only when the recorder is
    enabled; a role can disable capture entirely via its schema flag.
    """

    def record(
        self,
        urls: List[str],
        *,
        active: int = 0,
        storage_state: Optional[Dict[str, Any]] = None,
        tool: str = "",
    ) -> None:
        """Record the browser's final open-tab URLs + optional storage_state.

        Args:
            urls: The URLs of the currently open tabs (page order).
            active: Index of the active tab within ``urls``.
            storage_state: Playwright ``storage_state`` dict ({cookies, origins})
                carrying the logged-in session, or None when not captured.
            tool: Name of the tool performing the capture (for the record).
        """
        ...
