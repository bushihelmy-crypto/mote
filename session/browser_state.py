"""Browser-state recorder — persists the live browser's tabs for resume.

The persistent ``WebBrowser`` tool maintains a live Playwright browser whose open
tabs, navigated URLs, and logged-in session (cookies / localStorage) live *in
that process*. That process is runtime-only (it cannot cross a checkpoint), so a
plain resume starts a clean browser. This recorder captures the browser's final
**browsing state** (open-tab URLs + active tab + an optional ``storage_state``)
into the same ``rollout.jsonl`` as the session's other events, so resume can
re-open the same tabs seeded with the saved session — *without* re-running any of
the original navigation/click actions (no replaying form submits, purchases, …).

Note: only the page URLs + storage are captured. Live DOM state, scroll
position, and in-flight JS are NOT preserved; the model re-sees its prior actions
in the replayed message history and re-establishes any page state it needs.

Privacy: ``storage_state`` may carry sensitive cookies. Capture is gated by
``enabled`` (the role's ``record_browser_state`` schema flag), so a role can opt
out of writing session cookies into the rollout entirely.

Mirrors :class:`~mote.session.terminal_state.TerminalStateRecorder` and
:class:`~mote.session.kernel_state.KernelStateRecorder`: conforms to
``mote.common.interface.BrowserStateStore``, shares the session's
:class:`SessionLog`, is best-effort (never raises into the tool), and is gated by
``enabled`` (off during resume replay).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mote.common.logs import log_class, logger
from mote.session.events import BrowserStateEvent
from mote.session.log import SessionLog


@log_class(level="DEBUG", exclude={"record"})
class BrowserStateRecorder:
    """Appends the browser's final open-tab URLs + storage to the session log.

    Conforms to ``mote.common.interface.BrowserStateStore``. Shares the
    session's :class:`SessionLog` so the browser-state event interleaves with the
    rest of the rollout. ``enabled`` gates recording (off during resume replay,
    or per-role via the schema flag). Last-write-wins: only the most recent event
    matters on replay. Independent of the terminal/kernel state events so a
    session's shell, kernel, and browser restore separately.
    """

    def __init__(self, log: SessionLog, *, enabled: bool = True):
        self._log = log
        self.enabled = enabled

    @property
    def log(self) -> SessionLog:
        return self._log

    def record(
        self,
        urls: List[str],
        *,
        active: int = 0,
        storage_state: Optional[Dict[str, Any]] = None,
        tool: str = "",
    ) -> None:
        """Append a :class:`BrowserStateEvent` (best-effort, never raises)."""
        if not self.enabled:
            return
        try:
            self._log.append(
                BrowserStateEvent(
                    urls=list(urls),
                    active=active,
                    storage_state=storage_state,
                    tool=tool,
                )
            )
        except Exception as exc:  # noqa: BLE001 — recording must not break the tool
            logger.warning(f"BrowserStateRecorder: failed to record browser state: {exc}")


__all__ = ["BrowserStateRecorder"]
