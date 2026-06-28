"""WebBrowser — one persistent web browser the model drives across calls.

The browser sibling of the persistent :class:`Terminal` / :class:`Python` tools:
there is **one implicit browser per Role session** (no browser id to track, like
a notebook kernel), and the model drives it by issuing actions:

- ``navigate`` — go to a URL in the active tab.
- ``click`` — click the element matched by a CSS selector.
- ``type`` — fill a field (selector + text).
- ``read`` — return the active tab's visible text.
- ``screenshot`` — capture the active tab as an image (shown to the model).
- ``eval`` — run a JavaScript expression and return its result.
- ``back`` — navigate back in history.
- ``tabs`` / ``new_tab`` / ``switch_tab`` / ``close_tab`` — manage tabs.
- ``close`` — shut the browser down.

The open tabs, navigated URLs, and logged-in session (cookies / localStorage)
persist across calls, so you build up browsing state step by step.

The live :class:`BrowserSession` is owned by the Role: it is stored on the Role's
``RoleState`` (via the ``get_tool_session`` / ``set_tool_session`` capabilities)
rather than a process-global singleton, so each Role's browser is isolated and
torn down with it.
"""
from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable, ClassVar, Optional

from metagpt.common.logs import logger
from metagpt.common.prompt.tools import WEB_BROWSER_DESCRIPTION
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.dependency._browser import BrowserSession
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError, ToolResult


async def _noop_ask(_question: str) -> str:
    """Default ``ask_human`` stub for a tool bound without a Role (unit tests)."""
    return ""


@register_tool
class WebBrowser(BaseTool):
    """Drive a persistent web browser (one per session)."""

    name = "WebBrowser"
    aliases = ["browser"]
    max_result_size_chars: ClassVar[int] = 30_000
    description = WEB_BROWSER_DESCRIPTION
    requires = (
        "get_cwd",
        "get_tool_session",
        "set_tool_session",
        "get_browser_headless",
        "record_browser_state",
        "take_pending_browser_restore",
        "ask_human",
    )
    # Navigates and executes JavaScript on arbitrary pages.
    risk_level = "high"
    # Holds a live Playwright browser on RoleState between calls.
    stateful = True

    # Injected from Role by bind(): the cwd accessor + the per-Role tool-session
    # store (where the live BrowserSession is kept across calls).
    get_cwd: Callable[[], str] = staticmethod(lambda: "")
    get_tool_session: Callable[[str], Any]
    set_tool_session: Callable[[str, Any], None]
    # Capability accessor returning the role's ``browser_headless`` flag (True =>
    # run headless, the default). Defaults to a True stub so a tool bound without
    # a Role (unit tests) launches headless.
    get_browser_headless: Callable[[], bool] = staticmethod(lambda: True)
    # Capability accessors for session-resume browser-state restore:
    #   record_browser_state — persist (urls, active, storage_state) into the
    #     rollout after an action settles (so resume can re-open the tabs).
    #   take_pending_browser_restore — pop the state staged by resume_session
    #     (or None); applied once when a fresh browser launches.
    # Both default to no-op stubs so a tool bound without a Role (unit tests)
    # still runs (no recording, no restore).
    record_browser_state: Callable[..., None] = staticmethod(lambda *a, **k: None)
    take_pending_browser_restore: Callable[[], Optional[dict]] = staticmethod(lambda: None)

    # Human text channel (Role.ask_human): used by the ``assist`` action to pause
    # automation and let the user complete a step only a person can do (scan a
    # login QR code, enter an SMS / 2FA code). Defaults to a no-op stub returning
    # "" so a tool bound without a Role (unit tests) still runs.
    ask_human: Callable[[str], Awaitable[str]] = staticmethod(lambda q: _noop_ask(q))

    async def _ensure_session(self) -> BrowserSession:
        """Return this Role's live browser, launching a fresh one if needed.

        The session is stored on RoleState keyed by the tool name; a previously
        stored browser that has since died is dropped and replaced. On a resumed
        session the fresh browser re-opens the saved tabs (seeded with the stored
        session) — consumed once (the accessor clears it).
        """
        session = self.get_tool_session(self.name)
        if session is not None and not session.closed:
            return session
        if session is not None:
            session.kill()  # previous browser died — start fresh
        cwd = self.get_cwd() if self.get_cwd is not None else ""
        headless = self.get_browser_headless() if self.get_browser_headless is not None else True
        # On resume, the staged state carries the storage_state to seed the new
        # context with the logged-in session before re-opening tabs.
        pending = self.take_pending_browser_restore()
        storage_state = pending.get("storage_state") if pending else None
        session = BrowserSession(session_key=self.session_id, cwd=cwd or None, headless=headless)
        await session.start(storage_state=storage_state)
        if pending:
            await session.restore_state(
                pending.get("urls", []),
                pending.get("active", 0),
                storage_state,
            )
        self.set_tool_session(self.name, session)
        return session

    async def call(
        self,
        *,
        action: str = "read",
        url: str = "",
        selector: str = "",
        text: str = "",
        expression: str = "",
        index: int = 0,
        clear: bool = True,
        fields: Optional[dict] = None,
        schema: Optional[dict] = None,
        submit: str = "",
        prompt: str = "",
    ) -> Any:
        """Drive the session's persistent browser with a single *action*.

        Args:
            action: One of snapshot | navigate | click | type | read | screenshot |
                eval | wait | detect_forms | fill_form | extract | assist | back |
                tabs | new_tab | switch_tab | close_tab | close.
            url: Target URL for ``navigate`` and ``new_tab``.
            selector: Element to act on for ``click`` / ``type`` / ``wait`` —
                either an element index from the latest ``snapshot`` (``"5"`` or
                ``"[5]"``) or a raw CSS selector.
            text: Text to fill for the ``type`` action.
            expression: JavaScript expression for ``eval``, or the condition to
                poll for ``wait``.
            index: Tab index for ``switch_tab`` and ``close_tab``.
            clear: For ``type`` — replace the field's value (True, default) or
                append to it (False).
            fields: For ``fill_form`` — a ``{selector_or_index: value}`` mapping
                of fields to fill in one shot.
            schema: For ``extract`` — a ``{key: "selector[@attr]"}`` mapping; each
                key yields the matched element's text (or ``@attr`` value).
            submit: For ``fill_form`` — an optional selector/index to click after
                filling (submits the form).
            prompt: For ``assist`` — the instruction shown to the user describing
                what to complete in the browser window (e.g. "scan the login QR
                code", "enter the SMS code").
        """
        action = (action or "").strip().lower()

        if action == "close":
            session = self.get_tool_session(self.name)
            if session is None:
                return "[no browser to close]"
            await session.shutdown()
            self.set_tool_session(self.name, None)
            return "[browser closed]"

        try:
            session = await self._ensure_session()
            result = await self._dispatch(
                session,
                action,
                url,
                selector,
                text,
                expression,
                index,
                clear,
                fields,
                schema,
                submit,
                prompt,
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Error running web browser: {e}")

        # After the action settles, snapshot the browsing state for resume.
        # Best-effort; never breaks the call. Skipped for the ``screenshot``
        # capture itself only insofar as that path returns early below.
        await self._record_state(session)
        return result

    async def _dispatch(
        self,
        session: BrowserSession,
        action: str,
        url: str,
        selector: str,
        text: str,
        expression: str,
        index: int,
        clear: bool = True,
        fields: Optional[dict] = None,
        schema: Optional[dict] = None,
        submit: str = "",
        prompt: str = "",
    ) -> Any:
        """Route *action* to the matching engine method, returning its result."""
        if action == "snapshot":
            return await session.snapshot()
        if action == "navigate":
            if not url:
                raise ToolError("Error: 'navigate' requires a url.")
            return await session.navigate(url)
        if action == "click":
            if not selector:
                raise ToolError(
                    "Error: 'click' requires a selector (an element index from the "
                    "latest snapshot like '5', or a CSS selector)."
                )
            return await session.click(selector)
        if action == "type":
            if not selector:
                raise ToolError(
                    "Error: 'type' requires a selector (an element index from the "
                    "latest snapshot like '5', or a CSS selector)."
                )
            return await session.type_text(selector, text, clear=clear)
        if action == "wait":
            if not selector and not expression:
                raise ToolError("Error: 'wait' requires a selector or an expression to wait for.")
            return await session.wait(selector=selector, expression=expression)
        if action == "detect_forms":
            return await session.detect_forms()
        if action == "fill_form":
            if not fields:
                raise ToolError("Error: 'fill_form' requires a 'fields' mapping of " "{selector_or_index: value}.")
            return await session.fill_form(fields, submit=submit)
        if action == "extract":
            if not schema:
                raise ToolError("Error: 'extract' requires a 'schema' mapping of " "{key: 'selector[@attr]'}.")
            return await session.extract(schema)
        if action == "assist":
            if not prompt:
                raise ToolError(
                    "Error: 'assist' requires a 'prompt' describing what the user "
                    "should complete in the browser window (e.g. 'scan the login "
                    "QR code', 'enter the SMS code')."
                )
            return await session.assist(
                prompt,
                ask_human=self.ask_human,
                headless=self.get_browser_headless(),
            )
        if action == "read":
            return await session.read()
        if action == "screenshot":
            png = await session.screenshot()
            b64 = base64.b64encode(png).decode("ascii")
            return ToolResult(
                output="[screenshot of the active tab; shown below]",
                images=[b64],
                data={"type": "screenshot", "bytes": len(png)},
            )
        if action == "eval":
            if not expression:
                raise ToolError("Error: 'eval' requires an expression.")
            return await session.eval_js(expression)
        if action == "back":
            return await session.back()
        if action == "tabs":
            return await session.tabs()
        if action == "new_tab":
            return await session.new_tab(url or None)
        if action == "switch_tab":
            return session.switch_tab(index)
        if action == "close_tab":
            return await session.close_tab(index)
        raise ToolError(
            f"Error: unknown browser action '{action}'. Use snapshot | navigate | "
            f"click | type | wait | detect_forms | fill_form | extract | assist | "
            f"read | screenshot | eval | back | tabs | new_tab | switch_tab | "
            f"close_tab | close."
        )

    async def _record_state(self, session: BrowserSession) -> None:
        """Snapshot the browsing state into the rollout (best-effort)."""
        try:
            state = await session.capture_state()
        except Exception as exc:  # noqa: BLE001 — capture must not break the call
            logger.debug(f"web_browser: session state capture failed: {exc}")
            state = None
        if state is None:
            return
        urls, active, storage_state = state
        recorder = getattr(self, "record_browser_state", None)
        if recorder is not None:
            recorder(urls, active=active, storage_state=storage_state, tool=self.name)

    def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's browser (idempotent)."""
        session = self.get_tool_session(self.name)
        if session is not None:
            session.kill()
            self.set_tool_session(self.name, None)
