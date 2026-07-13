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
from typing import Any, ClassVar, Optional

from mote.common.logs import logger
from mote.common.prompt.tools import WEB_BROWSER_DESCRIPTION
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import (
    AskUser,
    GetBrowserHeadless,
    GetBrowserLocale,
    GetBrowserProxy,
    GetBrowserStealth,
    GetCwd,
    GetToolSession,
    RecordBrowserState,
    SetToolSession,
    TakePendingBrowserRestore,
)
from mote.executor.dependency._browser import BrowserSession
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolError, ToolMedia, ToolResult

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_BROWSER_FAILED = "Error running web browser: {error}"
_MSG_NAVIGATE_REQUIRES_URL = "Error: 'navigate' requires a url."
_MSG_CLICK_REQUIRES_SELECTOR = (
    "Error: 'click' requires a selector (an element index from the " "latest snapshot like '5', or a CSS selector)."
)
_MSG_TYPE_REQUIRES_SELECTOR = (
    "Error: 'type' requires a selector (an element index from the " "latest snapshot like '5', or a CSS selector)."
)
_MSG_WAIT_REQUIRES = "Error: 'wait' requires a selector or an expression to wait for."
_MSG_FILL_FORM_REQUIRES = "Error: 'fill_form' requires a 'fields' mapping of {selector_or_index: value}."
_MSG_EXTRACT_REQUIRES = "Error: 'extract' requires a 'schema' mapping of {key: 'selector[@attr]'}."
_MSG_ASSIST_REQUIRES = (
    "Error: 'assist' requires a 'prompt' describing what the user "
    "should complete in the browser window (e.g. 'scan the login "
    "QR code', 'enter the SMS code')."
)
_MSG_EVAL_REQUIRES = "Error: 'eval' requires an expression."
_MSG_UNKNOWN_ACTION = (
    "Error: unknown browser action '{action}'. Use snapshot | navigate | "
    "click | type | wait | detect_forms | fill_form | extract | assist | "
    "read | screenshot | eval | back | tabs | new_tab | switch_tab | "
    "close_tab | close."
)


async def _noop_ask(_question: str) -> str:
    """Default ``ask_user`` stub for a tool bound without a Role (unit tests)."""
    return ""


@register_tool
class WebBrowser(BaseTool):
    """Drive a persistent web browser (one per session)."""

    name = "WebBrowser"
    aliases = ["browser"]
    # Hard opt-out of the executor's persist/truncate layer: the engine already
    # self-bounds page text at ``TEXT_MAX_CHARS`` (10M), so we don't want the
    # 50k default clamp persisting reads to disk. ``inf`` is the sanctioned
    # opt-out in ``persistence_threshold`` (a plain 10M would be clamped to 50k).
    max_result_size_chars: ClassVar[float] = float("inf")
    description = WEB_BROWSER_DESCRIPTION
    requires = (
        "get_cwd",
        "get_tool_session",
        "set_tool_session",
        "get_browser_headless",
        "get_browser_stealth",
        "get_browser_locale",
        "get_browser_proxy",
        "record_browser_state",
        "take_pending_browser_restore",
        "ask_user",
    )
    # Navigates and executes JavaScript on arbitrary pages.
    risk_level = "high"
    # Holds a live Playwright browser on RoleState between calls.
    stateful = True

    # Injected from Role by bind(): the cwd accessor + the per-Role tool-session
    # store (where the live BrowserSession is kept across calls).
    get_cwd: GetCwd = staticmethod(lambda: "")
    get_tool_session: GetToolSession
    set_tool_session: SetToolSession
    # Capability accessor returning the role's ``browser_headless`` flag (True =>
    # run headless, the default). Defaults to a True stub so a tool bound without
    # a Role (unit tests) launches headless.
    get_browser_headless: GetBrowserHeadless = staticmethod(lambda: True)
    # Capability accessor returning the role's ``browser_stealth`` flag (True =>
    # apply opt-in anti-bot-detection). Defaults to a False stub so a tool bound
    # without a Role (unit tests) launches with no fingerprint overrides.
    get_browser_stealth: GetBrowserStealth = staticmethod(lambda: False)
    # Capability accessor returning the role's ``browser_locale`` setting
    # ("auto"/"en"/"zh"), selecting the stealth fingerprint's locale bundle.
    # Defaults to an "auto" stub so a tool bound without a Role (unit tests)
    # lets the engine infer the locale from the host env.
    get_browser_locale: GetBrowserLocale = staticmethod(lambda: "auto")
    # Capability accessor returning the role's ``browser_proxy`` URL (empty =>
    # direct connection). Defaults to an empty stub so a tool bound without a
    # Role (unit tests) connects directly.
    get_browser_proxy: GetBrowserProxy = staticmethod(lambda: "")
    # Capability accessors for session-resume browser-state restore:
    #   record_browser_state — persist (urls, active, storage_state) into the
    #     rollout after an action settles (so resume can re-open the tabs).
    #   take_pending_browser_restore — pop the state staged by resume_session
    #     (or None); applied once when a fresh browser launches.
    # Both default to no-op stubs so a tool bound without a Role (unit tests)
    # still runs (no recording, no restore).
    record_browser_state: RecordBrowserState = staticmethod(lambda *a, **k: None)
    take_pending_browser_restore: TakePendingBrowserRestore = staticmethod(lambda: None)

    # Human text channel (Role.ask_user): used by the ``assist`` action to pause
    # automation and let the user complete a step only a person can do (scan a
    # login QR code, enter an SMS / 2FA code). Defaults to a no-op stub returning
    # "" so a tool bound without a Role (unit tests) still runs.
    ask_user: AskUser = staticmethod(lambda q: _noop_ask(q))

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
        stealth = self.get_browser_stealth() if self.get_browser_stealth is not None else False
        browser_locale = self.get_browser_locale() if self.get_browser_locale is not None else "auto"
        proxy = self.get_browser_proxy() if self.get_browser_proxy is not None else ""
        # On resume, the staged state carries the storage_state to seed the new
        # context with the logged-in session before re-opening tabs.
        pending = self.take_pending_browser_restore()
        storage_state = pending.get("storage_state") if pending else None
        session = BrowserSession(
            session_key=self.session_id,
            cwd=cwd or None,
            headless=headless,
            stealth=stealth,
            browser_locale=browser_locale,
            proxy=proxy,
        )
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
        extract_links: bool = False,
        extract_images: bool = False,
        interactive_only: bool = False,
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
            extract_links: For ``read`` — keep hyperlink URLs (default False drops
                them, rendering links as plain text). Set True when you need a
                URL to navigate to.
            extract_images: For ``read`` — keep image src URLs (default False
                drops images entirely). Set True when you need to inspect an
                image src.
            interactive_only: For ``snapshot`` — drop the interleaved prose text
                and emit only the clickable ``[N]`` element lines, for a compact
                controls-only view when tokens are tight (default False returns
                the full unified tree of prose + refs).
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
                extract_links,
                extract_images,
                interactive_only,
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_BROWSER_FAILED.format(error=e))

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
        extract_links: bool = False,
        extract_images: bool = False,
        interactive_only: bool = False,
    ) -> Any:
        """Route *action* to the matching engine method, returning its result."""
        if action == "snapshot":
            return await session.snapshot(interactive_only=interactive_only)
        if action == "navigate":
            if not url:
                raise ToolError(_MSG_NAVIGATE_REQUIRES_URL)
            return await session.navigate(url)
        if action == "click":
            if not selector:
                raise ToolError(_MSG_CLICK_REQUIRES_SELECTOR)
            return await session.click(selector)
        if action == "type":
            if not selector:
                raise ToolError(_MSG_TYPE_REQUIRES_SELECTOR)
            return await session.type_text(selector, text, clear=clear)
        if action == "wait":
            if not selector and not expression:
                raise ToolError(_MSG_WAIT_REQUIRES)
            return await session.wait(selector=selector, expression=expression)
        if action == "detect_forms":
            return await session.detect_forms()
        if action == "fill_form":
            if not fields:
                raise ToolError(_MSG_FILL_FORM_REQUIRES)
            return await session.fill_form(fields, submit=submit)
        if action == "extract":
            if not schema:
                raise ToolError(_MSG_EXTRACT_REQUIRES)
            return await session.extract(schema)
        if action == "assist":
            if not prompt:
                raise ToolError(_MSG_ASSIST_REQUIRES)
            return await session.assist(
                prompt,
                ask_user=self.ask_user,
                headless=self.get_browser_headless(),
            )
        if action == "read":
            return await session.read(extract_links=extract_links, extract_images=extract_images)
        if action == "screenshot":
            png = await session.screenshot()
            b64 = base64.b64encode(png).decode("ascii")
            return ToolResult(
                output="[screenshot of the active tab; shown below]",
                media=[ToolMedia(kind="image", b64=b64, ref="", mime="image/png")],
                data={"type": "screenshot", "bytes": len(png)},
            )
        if action == "eval":
            if not expression:
                raise ToolError(_MSG_EVAL_REQUIRES)
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
        raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))

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
