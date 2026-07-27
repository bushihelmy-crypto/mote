"""WebBrowser — one persistent web browser the model drives across calls.

The browser sibling of the persistent :class:`Terminal` / :class:`Python` tools:
there is **one implicit browser per Role session** (no browser id to track, like
a notebook kernel), and the model drives it by issuing actions:

- ``navigate`` — go to a URL in the active tab.
- ``click`` — click the element matched by a CSS selector.
- ``type`` — fill a field (selector + text).
- ``read`` — return the active tab's visible text.
- ``read_image`` — read one page image (by selector) as text via a vision model.
- ``screenshot`` — capture the active tab as an image (shown to the model).
- ``eval`` — run a JavaScript expression and return its result.
- ``back`` — navigate back in history.
- ``tabs`` / ``new_tab`` / ``switch_tab`` / ``close_tab`` — manage tabs.
- ``close`` — shut the browser down.

The open tabs, navigated URLs, and logged-in session (cookies / localStorage)
persist across calls, so you build up browsing state step by step.

The live :class:`BrowserSession` is owned by the Role's ``RuntimeHost`` rather
than a process-global singleton, so each Role's browser is isolated, fenced,
revisioned, handed off, and torn down through the shared runtime lifecycle.
"""
from __future__ import annotations

from typing import Any, ClassVar, Optional

from mote.contracts.errors.runtimes import ManagedRuntimeNotFoundError
from mote.contracts.models.capabilities import supports_vision
from mote.contracts.permissions import PermissionDecision
from mote.contracts.runtimes import RuntimeAccessMode
from mote.product.toolsets.builtin.runtime_action import handoff_permission, is_handoff_action, run_handoff_action
from mote.runtime.artifacts.media import publish_media_artifact
from mote.runtime.errors import ToolNotConfiguredError
from mote.runtime.secrets.refs import SecretRefError, expand_secret_refs
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import (
    DescribeImage,
    GetArtifactPublisher,
    GetBrowserCdpEndpoint,
    GetBrowserClientCerts,
    GetBrowserLocale,
    GetBrowserProfile,
    GetBrowserProxy,
    GetBrowserStealth,
    GetCwd,
    GetDefaultModel,
    GetRuntimeHost,
    GetSecret,
    HandoffRuntime,
    LoadBrowserProfile,
    SaveBrowserProfile,
)
from mote.runtime.tools.dependency._browser import BrowserSession
from mote.runtime.tools.dependency._browser_runtime import BrowserRuntimeDriver
from mote.runtime.tools.dependency._video import looks_like_video_path
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolError, ToolMedia, ToolResult

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
_MSG_EVAL_REQUIRES = "Error: 'eval' requires an expression."
_MSG_READ_IMAGE_REQUIRES = (
    "Error: 'read_image' requires a selector (an image element index from the "
    "latest snapshot like '5', or a CSS selector for an <img>)."
)
_MSG_IMAGE_UNAVAILABLE = (
    "Image understanding is unavailable: no vision-capable model is configured. "
    "Configure a multimodal model for the 'image_description' task (or as the "
    "default model) to read page images as text."
)
_READ_IMAGE_HEADER = "Image reading of {target}:"
_MSG_SCREENSHOT_MODEL_UNSUPPORTED = (
    "Cannot take a screenshot: the default model '{model}' is not vision-capable, "
    "so the captured image would never reach it. Configure a multimodal (vision) "
    "model as models.default, or use the 'read' action to get the page's text."
)
_MSG_NAVIGATE_IS_VIDEO = (
    "'{url}' is a video URL. Navigating a browser to a raw video only loads a "
    "player you cannot see. Download it to a local file with yt-dlp, then open "
    "that file with the Read tool — it decomposes a local video into timestamped "
    "frames (shown to you as images) plus a timestamped transcript:\n"
    "  yt-dlp -o clip.mp4 <url>\n"
    "then: Read clip.mp4"
)
_MSG_UNKNOWN_ACTION = (
    "Error: unknown browser action '{action}'. Use snapshot | navigate | "
    "click | type | wait | detect_forms | fill_form | extract | read | read_image | "
    "screenshot | eval | back | tabs | new_tab | "
    "switch_tab | close_tab | handoff | close."
)
_RUNTIME = "browser:default"


async def _no_vision(_artifact, *, prompt: str = "") -> str:
    """Default ``describe_image`` stub — no vision model bound (unit tests).

    Raises ``NotImplementedError`` so ``read_image`` surfaces the same
    :class:`ToolNotConfiguredError` as a genuinely non-vision model.
    """
    raise NotImplementedError("no vision-capable model bound")


@register_tool
class WebBrowser(BaseTool):
    """Drive a persistent web browser (one per session)."""

    name = "WebBrowser"
    aliases = ["browser"]
    # Recall synonyms for tool-search: ways a model asks to open/read a known
    # page that the summary ("drive a web browser") does not literally contain.
    keywords: ClassVar[list[str]] = [
        "website",
        "url",
        "webpage",
        "navigate",
        "open link",
        "visit",
        "网页",
        "网站",
        "打开链接",
        "浏览器",
        "浏览",
    ]
    # Hard opt-out of the executor's persist/truncate layer: the engine already
    # self-bounds page text at ``TEXT_MAX_CHARS`` (10M), so we don't want the
    # 50k default clamp persisting reads to disk. ``inf`` is the sanctioned
    # opt-out in ``persistence_threshold`` (a plain 10M would be clamped to 50k).
    max_result_size_chars: ClassVar[float] = float("inf")
    requires = (
        "get_cwd",
        "get_runtime_host",
        "get_browser_stealth",
        "get_browser_locale",
        "get_browser_proxy",
        "get_browser_cdp_endpoint",
        "get_browser_profile",
        "load_browser_profile",
        "save_browser_profile",
        "get_browser_client_certs",
        "get_secret",
        "describe_image",
        "get_default_model",
        "get_artifact_publisher",
        "handoff_runtime",
    )
    # Navigates and executes JavaScript on arbitrary pages.
    risk_level = "high"
    # Fronts one live Playwright browser managed by RuntimeHost between calls.
    stateful = True

    # Injected from Role by bind(): cwd seeds the browser and RuntimeHost owns it.
    get_cwd: GetCwd = staticmethod(lambda: "")
    get_runtime_host: GetRuntimeHost
    get_artifact_publisher: GetArtifactPublisher
    handoff_runtime: HandoffRuntime
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
    # Optional endpoint for an already-running, user-owned Chrome. Attaching
    # retains its real browser profile rather than launching headless Chromium.
    get_browser_cdp_endpoint: GetBrowserCdpEndpoint = staticmethod(lambda: "")
    # Durable browser-login profile capabilities:
    #   get_browser_profile — the configured profile name (empty => ephemeral,
    #     the pre-profile behavior);
    #   load_browser_profile — decrypt a saved storage_state for that name (or
    #     None), used to seed a fresh session with a persisted login;
    #   save_browser_profile — persist the session's storage_state back into the
    #     encrypted profile after an action settles.
    # Default stubs (no profile / no-op) so a tool bound without a Role (unit
    # tests) stays fully ephemeral.
    get_browser_profile: GetBrowserProfile = staticmethod(lambda: "")
    load_browser_profile: LoadBrowserProfile = staticmethod(lambda _name: None)
    save_browser_profile: SaveBrowserProfile = staticmethod(lambda _name, _state: None)
    # Client TLS certs (mutual-TLS logins): Playwright ``client_certificates``
    # entries; each ``passphrase`` may be a secret placeholder expanded at
    # launch. The managed Browser Runtime itself always remains private and
    # headless; Handoff visibility belongs to the independent presenter.
    get_browser_client_certs: GetBrowserClientCerts = staticmethod(lambda: [])
    # Named-secret resolver (Role.get_secret): the ``type`` / ``fill_form``
    # actions expand ``<secret:KEY>`` / ``<agent-vault:KEY>`` / ``<totp:KEY>``
    # placeholders the model writes into a field to the real credential AT FILL
    # TIME — so the plaintext never enters the model's context or the rollout.
    # Defaults to a None stub so a tool bound without a Role (unit tests) simply
    # finds no secret and a placeholder fails closed.
    get_secret: GetSecret = staticmethod(lambda _key: None)
    # Vision-model reader (Role.describe_image): used by the ``read_image``
    # action to turn a page image into text via an isolated multimodal call.
    # Defaults to a stub that signals "no vision model" so a tool bound without
    # a Role (unit tests) still runs and degrades cleanly.
    describe_image: DescribeImage = staticmethod(_no_vision)

    # Default (main think-loop) model name (Role.get_default_model): the
    # ``screenshot`` action attaches an image that rides the MAIN model's
    # request, so it checks ``supports_vision`` against this to refuse up-front.
    # Defaults to a None stub so a tool bound without a Role (unit tests) skips
    # the check and screenshots normally.
    get_default_model: GetDefaultModel = staticmethod(lambda: None)

    async def _ensure_runtime(self) -> None:
        """Atomically create this Role's implicit managed browser Runtime."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
            return
        except ManagedRuntimeNotFoundError:
            pass
        cwd = self.get_cwd() if self.get_cwd is not None else ""
        stealth = self.get_browser_stealth() if self.get_browser_stealth is not None else False
        browser_locale = self.get_browser_locale() if self.get_browser_locale is not None else "auto"
        proxy = self.get_browser_proxy() if self.get_browser_proxy is not None else ""
        cdp_endpoint = self.get_browser_cdp_endpoint() if self.get_browser_cdp_endpoint is not None else ""
        # Client TLS certs: resolve any secret-placeholder passphrase from the
        # vault (by key, never by value — same seam as ``type``/``fill_form``) so
        # the plaintext lives only in the launch kwargs, never in config/history.
        client_certs = self._resolve_client_certs()
        # A durable encrypted profile overrides storage from a same-session
        # logical checkpoint, which RuntimeHost supplies directly to the driver.
        profile = self.get_browser_profile()
        storage_state = self.load_browser_profile(profile) if profile else None
        driver = BrowserRuntimeDriver(
            session_key=self.session_id,
            cwd=cwd or None,
            stealth=stealth,
            browser_locale=browser_locale,
            proxy=proxy,
            cdp_endpoint=cdp_endpoint,
            client_certs=client_certs,
            storage_state=storage_state,
            persist_storage_state=not bool(profile),
        )
        await host.ensure(driver)

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
        message: str = "",
    ) -> Any:
        """Drive a persistent browser — navigate, click, fill forms, log in, read JS pages.

        One browser per session; open tabs, URLs, and logged-in session persist
        across calls. Actions:

        - snapshot — unified indented tree of prose + clickable elements (each
          tagged [5]) in reading order; a leading * marks elements new since the
          last snapshot; interactive_only=true drops prose for a compact
          controls-only list.
        - navigate url. click selector (an index like '5' or a CSS selector).
          type selector + text (clear=false appends). wait — block until a
          selector appears or a JS expression is truthy (dynamic/SPA content).
        - detect_forms — list forms + fillable fields. fill_form — {selector:
          value} mapping, optional submit selector. extract — {key:
          'selector[@attr]'} schema → JSON.
        - read — main content as pure Markdown prose (no [N] refs); links/images
          dropped by default (extract_links / extract_images to keep them).
          read_image — read ONE image as text via a vision model (selector +
          optional prompt). screenshot — capture as image. eval — run JS → JSON.
        - back (prefer over re-navigating). tabs — list. new_tab url. switch_tab
          index. close_tab index. close — shut down.
        - handoff — give the user exclusive control of the already-open browser
          and wait until control returns.

        Loop: snapshot, then click/type by index; re-snapshot after navigation or
        any DOM change (indices are only valid for the latest snapshot).

        Logging in is allowed. When credentials exist, a login menu lists the
        exact value to type per field — type it verbatim and the tool fills the
        real secret without it entering your context. Use ONLY the names shown,
        and the login method those credentials support (e.g. phone-and-code if
        only a phone is listed). Never invent a reference for an unlisted name (an
        unlisted ``<agent-vault:KEY>`` fails to resolve and aborts the fill). For
        a value you weren't given (password, one-time code), ask the user first,
        then pass the answer to ``type``/``fill_form``.

        Args:
            action: One of snapshot | navigate | click | type | read | read_image |
                screenshot | eval | wait | detect_forms | fill_form | extract |
                back | tabs | new_tab | switch_tab | close_tab |
                handoff | close.
            url: Target URL for ``navigate`` / ``new_tab``.
            selector: Element for ``click`` / ``type`` / ``wait`` / ``read_image``
                — an index from the latest ``snapshot`` (``"5"``/``"[5]"``) or a
                CSS selector.
            text: Text to fill for ``type``. A login-menu value fills as the real
                secret without entering your context.
            expression: JS for ``eval``, or the condition to poll for ``wait``.
            index: Tab index for ``switch_tab`` / ``close_tab``.
            clear: For ``type`` — replace the value (True) or append (False).
            fields: For ``fill_form`` — a ``{selector_or_index: value}`` mapping.
                A login-menu value fills as the real secret.
            schema: For ``extract`` — a ``{key: "selector[@attr]"}`` mapping; each
                key yields the element's text (or ``@attr`` value).
            submit: For ``fill_form`` — optional selector/index to click after
                filling.
            prompt: For ``read_image`` — optional instruction steering extraction
                (e.g. "transcribe the chart").
            extract_links: For ``read`` — keep hyperlink URLs (default False).
            extract_images: For ``read`` — keep image src URLs (default False).
            interactive_only: For ``snapshot`` — emit only clickable ``[N]`` lines
                (default False returns the full prose + refs tree).
            message: Optional instructions shown to the user during handoff.
        """
        action = (action or "").strip().lower()

        if action == "handoff":
            return await run_handoff_action(self.handoff_runtime, _RUNTIME, message=message)
        if action == "close":
            host = self.get_runtime_host()
            try:
                host.descriptor(_RUNTIME)
            except ManagedRuntimeNotFoundError:
                return "[no browser to close]"
            await host.close(_RUNTIME)
            return "[browser closed]"

        # Recognise a raw video URL and guide to "yt-dlp download + Read" BEFORE
        # launching a browser — navigating to a video only loads an unseeable
        # player, and the guide needs no live session. (A YouTube-style watch
        # PAGE is a real page, so only a direct video-file URL trips this; the
        # model downloads any yt-dlp-supported URL to a local file, then Reads it.)
        if action == "navigate" and url and looks_like_video_path(url):
            raise ToolError(_MSG_NAVIGATE_IS_VIDEO.format(url=url))

        try:
            await self._ensure_runtime()
            host = self.get_runtime_host()
            async with host.access(
                _RUNTIME,
                mode=RuntimeAccessMode.WRITE,
                owner_id=f"agent:{self.session_id}:browser",
            ) as access:
                driver = access.driver
                if not isinstance(driver, BrowserRuntimeDriver):
                    raise RuntimeError("browser runtime has an unexpected driver")
                result = await self._dispatch(
                    driver.session,
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
                driver.surface_changed()
                await self._persist_profile(driver.session)
                access.commit()
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_BROWSER_FAILED.format(error=e))

        return result

    def check_permissions(self, args: dict) -> PermissionDecision | None:
        if is_handoff_action(args):
            return handoff_permission()
        return None

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
            return await session.type_text(selector, self._resolve_secrets(text), clear=clear)
        if action == "wait":
            if not selector and not expression:
                raise ToolError(_MSG_WAIT_REQUIRES)
            return await session.wait(selector=selector, expression=expression)
        if action == "detect_forms":
            return await session.detect_forms()
        if action == "fill_form":
            if not fields:
                raise ToolError(_MSG_FILL_FORM_REQUIRES)
            resolved = {sel: self._resolve_secrets(val) for sel, val in fields.items()}
            return await session.fill_form(resolved, submit=submit)
        if action == "extract":
            if not schema:
                raise ToolError(_MSG_EXTRACT_REQUIRES)
            return await session.extract(schema)
        if action == "read":
            return await session.read(extract_links=extract_links, extract_images=extract_images)
        if action == "read_image":
            if not selector:
                raise ToolError(_MSG_READ_IMAGE_REQUIRES)
            png = await session.read_image(selector)
            artifact = await publish_media_artifact(
                self.get_artifact_publisher(),
                content=png,
                representation="png",
                kind="browser-image",
                mime_type="image/png",
                suggested_name="browser-image.png",
            )
            try:
                description = await self.describe_image(artifact, prompt=prompt.strip())
            except NotImplementedError:
                raise ToolNotConfiguredError(_MSG_IMAGE_UNAVAILABLE)
            header = _READ_IMAGE_HEADER.format(target=selector)
            return ToolResult(output=f"{header}\n{description}".strip())
        if action == "screenshot":
            model = self.get_default_model()
            if model is not None and not supports_vision(model):
                raise ToolNotConfiguredError(_MSG_SCREENSHOT_MODEL_UNSUPPORTED.format(model=model))
            png = await session.screenshot()
            artifact = await publish_media_artifact(
                self.get_artifact_publisher(),
                content=png,
                representation="png",
                kind="browser-screenshot",
                mime_type="image/png",
                suggested_name="browser-screenshot.png",
            )
            return ToolResult(
                output="[screenshot of the active tab; shown below]",
                media=[ToolMedia(kind="image", mime="image/png", artifact=artifact)],
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
            return await session.switch_tab(index)
        if action == "close_tab":
            return await session.close_tab(index)
        raise ToolError(_MSG_UNKNOWN_ACTION.format(action=action))

    def _resolve_secrets(self, value: Any) -> Any:
        """Expand any secret placeholder in a to-be-typed value at fill time.

        A model authors a field value like ``<agent-vault:xhs_password>`` or
        ``<totp:xhs_2fa>``; this substitutes the real credential from the vault
        (by key, never by value) just before it is typed into the page, so the
        plaintext lives only in this local return value — never in the model's
        context, the recorded tool-call args, or the rollout. A non-string (a
        checkbox bool, a number) or a value with no placeholder passes through
        untouched. An unresolved reference fails closed as an actionable
        ``ToolError`` rather than typing the literal placeholder text.
        """
        if not isinstance(value, str):
            return value
        try:
            return expand_secret_refs(value, get_secret=self.get_secret)
        except SecretRefError as exc:
            raise ToolError(str(exc)) from exc

    def _resolve_client_certs(self) -> list:
        """Return the role's client TLS certs with passphrases expanded.

        Each cert dict comes from ``get_browser_client_certs`` (Playwright shape:
        ``origin`` + ``certPath`` / ``keyPath`` / ``pfxPath`` / ``passphrase``).
        A ``passphrase`` may be a secret placeholder — resolved from the vault
        here, at launch time, via the same fail-closed seam as ``type`` — so the
        plaintext never rides config or history. An unresolved reference raises
        an actionable ``ToolError`` rather than launching with the literal.
        """
        certs = self.get_browser_client_certs() if self.get_browser_client_certs is not None else []
        out: list = []
        for cert in certs:
            resolved = dict(cert)
            if resolved.get("passphrase"):
                resolved["passphrase"] = self._resolve_secrets(resolved["passphrase"])
            out.append(resolved)
        return out

    async def _persist_profile(self, session: BrowserSession) -> None:
        """Persist login state independently from rollout checkpoint policy."""
        profile = self.get_browser_profile()
        if not profile:
            return
        try:
            state = await session.capture_state()
        except Exception:
            return
        if state is not None:
            self.save_browser_profile(profile, state[2])

    async def cleanup_session(self, session_id: str) -> None:
        """Tear down this Role's browser (idempotent)."""
        host = self.get_runtime_host()
        try:
            host.descriptor(_RUNTIME)
        except ManagedRuntimeNotFoundError:
            return
        await host.close(_RUNTIME)
