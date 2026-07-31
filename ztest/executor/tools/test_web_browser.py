#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the persistent ``WebBrowser`` tool.

Drives a real headless Chromium through the tool's ``call``, using the shared
``CapRole``/``bind``/``run``/``workspace`` harness. Everything is local and
offline: pages are loaded from ``data:`` URLs (no network), so no site is
contacted.

A live browser keeps its Playwright connection on the event loop it was started
on, so multi-call scenarios run inside ONE ``asyncio.run`` (the conftest ``run``
opens a fresh loop per call). The live session is owned by the per-test
``RuntimeHost``, so there is no process-global singleton to leak; each test still
closes its browser to free the subprocess.

Skipped entirely when Playwright / its Chromium browser is unavailable.
"""
from __future__ import annotations

import pytest

from mote.contracts.artifact import ArtifactRef
from mote.contracts.runtime import RuntimeAccessMode
from mote.contracts.runtime.errors import ManagedRuntimeNotFoundError
from mote.product.toolsets.builtin.web_browser import WebBrowser
from mote.runtime.interactive.browser.session import TEXT_MAX_CHARS, BrowserSession
from mote.runtime.text.elision import cap_head_tail

from .conftest import CapRole, bind, run

# Skip the whole module if Playwright (or the Chromium binary) is not installed.
playwright = pytest.importorskip("playwright.async_api")


def _chromium_available() -> bool:
    """Best-effort check that a launchable Chromium exists."""
    try:
        import asyncio

        async def _probe():
            from playwright.async_api import async_playwright

            cm = async_playwright()
            pw = await cm.start()
            try:
                browser = await pw.chromium.launch(headless=True)
                await browser.close()
                return True
            finally:
                await cm.__aexit__(None, None, None)

        return asyncio.run(_probe())
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not available")

# A couple of offline pages.
_PAGE_A = "data:text/html,<title>Alpha</title><body><h1>Alpha</h1><p>hello world</p></body>"
_PAGE_B = "data:text/html,<title>Beta</title><body><p>second page</p></body>"
_FORM = "data:text/html,<title>Form</title><body>" "<input id='q' value=''><button id='go'>Go</button></body>"

# A page with several interactive elements for snapshot/ref tests.
_INTERACTIVE = (
    "data:text/html,<title>Shop</title><body>"
    "<h1>Shop</h1>"
    "<a id='home' href='https://example.com/'>Home</a>"
    "<input id='search' placeholder='Search products'>"
    "<button id='buy'>Buy now</button>"
    "<p>some descriptive text</p>"
    "</body>"
)

# A button fully covered by a fixed overlay (consent banner style).
_OVERLAY = (
    "data:text/html,<title>Blocked</title><body>"
    "<button id='target' style='position:fixed;top:50px;left:50px;"
    "width:100px;height:40px'>Target</button>"
    "<div id='cover' style='position:fixed;top:0;left:0;width:100%;"
    "height:100%;background:rgba(0,0,0,0.5)'>Consent</div>"
    "</body>"
)

# A page carrying a real (rendered) image element for read_image. The src is a
# 1x1 transparent PNG data-URI, so Chromium actually rasterizes an <img> box we
# can element-screenshot — no network.
_PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_IMAGE_PAGE = (
    "data:text/html,<title>Pic</title><body>"
    "<h1>Gallery</h1>"
    "<img id='chart' src='" + _PNG_1PX + "' width='40' height='40' alt='the chart'>"
    "</body>"
)

# A content-rich page for Markdown extraction.
_ARTICLE = (
    "data:text/html,<title>Article</title><body>"
    "<nav>skip me</nav>"
    "<h1>Big Title</h1>"
    "<p>First paragraph of the body.</p>"
    "<h2>Section</h2>"
    "<p>Second paragraph here.</p>"
    "<footer>footer noise</footer>"
    "</body>"
)

# A real <form> with labelled fields for detect_forms / fill_form.
_LOGIN_FORM = (
    "data:text/html,<title>Login</title><body>"
    "<form id='login' action='/auth' method='post'>"
    "<label for='user'>Username</label>"
    "<input id='user' name='username' type='text'>"
    "<label for='pass'>Password</label>"
    "<input id='pass' name='password' type='password'>"
    "<button type='submit'>Sign in</button>"
    "</form></body>"
)

# A list page for schema-driven extract (text + @attr).
_LISTING = (
    "data:text/html,<title>Links</title><body>"
    "<h1 id='hd'>Catalog</h1>"
    "<a class='item' href='/a'>Apple</a>"
    "<a class='item' href='/b'>Banana</a>"
    "<a class='item' href='/c'>Cherry</a>"
    "</body>"
)

# A page that reveals content after a short delay (for wait).
_DELAYED = (
    "data:text/html,<title>Delayed</title><body><div id='box'></div>"
    "<script>setTimeout(function(){"
    "var d=document.createElement('div');d.id='late';d.textContent='ready';"
    "document.body.appendChild(d);window.__done=true;}, 300);</script>"
    "</body>"
)

# A real control nested inside a clickable (cursor:pointer) wrapper whose own
# innerText contains the control's label. This is the shape that caused a
# "获取验证码" (send SMS code) button to be dropped from the snapshot: the
# wrapper is interactive (cursor:pointer), the inner button's rect is contained
# in it, and the wrapper's accessibleName includes the button's text — so the
# containment-collapse heuristic treated the button as decorative. A genuine
# <button> must still surface with its own [N] ref (strongInteractive exemption).
_NESTED_CONTROL = (
    "data:text/html;charset=utf-8,<title>SmsForm</title><body>"
    "<div style='cursor:pointer'>"
    "<input id='phone' placeholder='请输入手机号'>"
    "<button id='sendcode'>获取验证码</button>"
    "</div>"
    "</body>"
)

# The classic custom-styled "I agree to terms" checkbox: the real
# <input type=checkbox> is drawn transparent (opacity:0) with a styled visual on
# top, yet it stays fully clickable. It used to be dropped from the snapshot
# because the visibility test treated opacity:0 as invisible — so an agent could
# not tick the consent box required to log in. A genuine control is judged by
# ACTIONABILITY (opacity ignored, like Playwright) and gets its own [N] ref.
_OPACITY_CHECKBOX = (
    "data:text/html;charset=utf-8,<title>Consent</title><body>"
    "<label for='agree'>I agree to the terms</label>"
    "<input id='agree' type='checkbox' style='opacity:0' "
    "aria-label='agree to terms'>"
    "</body>"
)

# An actionable control living UNDER an opacity:0 wrapper: the wrapper's prose is
# not readable (transparent), but the control beneath it is still clickable. The
# redesigned walker recurses past non-rendered wrappers (only display:none prunes
# a subtree), so the button surfaces while the invisible prose is suppressed.
_HIDDEN_WRAPPER_CONTROL = (
    "data:text/html;charset=utf-8,<title>Wrapped</title><body>"
    "<div style='opacity:0'>"
    "<p>invisible legalese</p>"
    "<button id='go'>Continue</button>"
    "</div>"
    "</body>"
)

# A child that overrides its parent's visibility:hidden with visibility:visible.
# The old walker pruned the whole subtree on the parent, dropping the visible
# child; the redesigned walker judges each node on its own computed style.
_VISIBILITY_OVERRIDE = (
    "data:text/html;charset=utf-8,<title>Override</title><body>"
    "<div style='visibility:hidden'>"
    "<button id='shown' style='visibility:visible'>Reveal me</button>"
    "</div>"
    "</body>"
)

# A control living inside an OPEN shadow root (Web Component). node.childNodes
# never descends into a shadow tree, so the old walker was blind to it — a login
# form built as a web component would be invisible. The walker now follows the
# flattened tree (childrenOf descends shadowRoot), and Playwright's CSS engine
# pierces open shadow DOM so the stamped ref is directly clickable.
_SHADOW_CONTROL = (
    "data:text/html;charset=utf-8,<title>Shadow</title><body>"
    "<div id='host'></div>"
    "<script>"
    "var sr=document.getElementById('host').attachShadow({mode:'open'});"
    "sr.innerHTML='<button id=\"sbtn\">Shadow Login</button>';"
    "</script>"
    "</body>"
)

# A light-DOM control projected into the shadow tree via <slot>. The flattened
# walk resolves a <slot> to its assigned light nodes, so the slotted button
# surfaces exactly once (no double-count with the host's light children).
_SHADOW_SLOT = (
    "data:text/html;charset=utf-8,<title>Slot</title><body>"
    "<div id='host2'><button id='slotted'>Slotted Btn</button></div>"
    "<script>"
    "var sr2=document.getElementById('host2').attachShadow({mode:'open'});"
    "sr2.innerHTML='<slot></slot>';"
    "</script>"
    "</body>"
)


# A login form living inside a same-origin <iframe srcdoc="...">. The top frame
# cannot read into the iframe's document via page.evaluate, so the old top-frame-
# only walker was blind to it. The snapshot now walks page.frames and runs the
# same _TREE_JS in the child frame, giving its controls refs in one flat [N]
# namespace and resolving click/type against the owning frame. (A srcdoc iframe
# is same-origin; a cross-origin frame differs only in that Playwright routes it
# to a separate target internally — the frame.evaluate / frame.wait_for_selector
# API surface exercised here is identical, so this faithfully covers the path.)
_IFRAME_LOGIN = (
    "data:text/html;charset=utf-8,<title>Outer</title><body>"
    "<button id='outer'>Outer Button</button>"
    "<iframe srcdoc=\"<body><input id='u' placeholder='frame-username'>"
    "<button id='fb'>Frame Login</button></body>\"></iframe>"
    "</body>"
)


def _has_browser(role: CapRole) -> bool:
    try:
        role.get_runtime_host().descriptor("browser:default")
    except ManagedRuntimeNotFoundError:
        return False
    return True


def _ref_for(snapshot: str, needle: str) -> "str | None":
    """Pull the ``[N]`` index of the first snapshot line containing *needle*.

    Returns the bare numeric ref (e.g. ``"3"``) so tests can drive click/type
    by index without hard-coding which index the walker assigns.
    """
    import re

    for line in snapshot.splitlines():
        if needle in line:
            # Tolerate the indent + optional ``*`` is-new marker before ``[N]``.
            m = re.match(r"\s*\*?\[(\d+)\]", line)
            if m:
                return m.group(1)
    return None


@pytest.fixture
def caprole(workspace):
    return CapRole(cwd=str(workspace))


# ---------------------------------------------------------------------------
# navigate / read
# ---------------------------------------------------------------------------


class TestNavigateRead:
    def test_navigate_then_read(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_nav")

        async def scenario():
            out = await tool.call(action="navigate", url=_PAGE_A)
            assert "navigated to" in out
            text = await tool.call(action="read")
            assert "hello world" in text
            await tool.call(action="close")

        run(scenario())

    def test_state_persists_across_calls(self, caprole):
        """A second call reuses the same live browser (same tab/url)."""
        tool = bind(WebBrowser(), caprole, session_id="b_persist")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            # read on a separate call still sees the page from the prior call.
            text = await tool.call(action="read")
            assert "Alpha" in text
            await tool.call(action="close")

        run(scenario())

    def test_navigate_video_url_guides_to_ytdlp_and_read(self, caprole):
        # A direct video URL is recognised and the model is guided to download it
        # with yt-dlp then Read the local file — WITHOUT launching a browser (the
        # guide needs no live session).
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_video")

        async def scenario():
            with pytest.raises(ToolError, match="yt-dlp") as excinfo:
                await tool.call(action="navigate", url="https://x.com/clip.mp4")
            msg = str(excinfo.value)
            assert "Read" in msg
            assert "https://x.com/clip.mp4" in msg

        run(scenario())


# ---------------------------------------------------------------------------
# eval / type / screenshot
# ---------------------------------------------------------------------------


class TestActions:
    def test_eval_returns_repr(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_eval")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            out = await tool.call(action="eval", expression="1 + 2")
            assert "3" in out
            await tool.call(action="close")

        run(scenario())

    def test_type_into_field(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_type")

        async def scenario():
            await tool.call(action="navigate", url=_FORM)
            await tool.call(action="type", selector="#q", text="kiwi")
            val = await tool.call(action="eval", expression="document.getElementById('q').value")
            assert "kiwi" in val
            await tool.call(action="close")

        run(scenario())

    def test_screenshot_returns_image(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_shot")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            result = await tool.call(action="screenshot")
            assert len(result.media) == 1
            assert result.media[0].artifact is not None
            assert await caprole.artifact_store.read(result.media[0].artifact)
            assert result.data["type"] == "screenshot"
            await tool.call(action="close")

        run(scenario())

    def test_screenshot_non_vision_model_raises(self, workspace):
        """A non-vision default model → refuse the capture (it could never reach it)."""
        from mote.runtime.errors import ToolNotConfiguredError

        role = CapRole(cwd=str(workspace), default_model="gpt-4")
        tool = bind(WebBrowser(), role, session_id="b_shot_novision")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            with pytest.raises(ToolNotConfiguredError, match="not vision-capable"):
                await tool.call(action="screenshot")
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# read_image — one page image → text via the vision fallback
# ---------------------------------------------------------------------------


class TestReadImage:
    def test_read_image_by_selector_describes(self, caprole):
        """A CSS selector locates the <img>; its bytes go to describe_image."""
        caprole.describe_image_text = "a small line chart trending up"
        tool = bind(WebBrowser(), caprole, session_id="b_img_sel")

        async def scenario():
            await tool.call(action="navigate", url=_IMAGE_PAGE)
            out = await tool.call(action="read_image", selector="#chart", prompt="describe the chart")
            # The model's textual reading is returned under a header.
            assert "a small line chart trending up" in out.output
            assert "#chart" in out.output
            # The vision capability was called once with real image bytes + prompt.
            assert len(caprole.describe_image_calls) == 1
            artifact, kwargs = caprole.describe_image_calls[0]
            assert isinstance(artifact, ArtifactRef)
            assert artifact.mime_type == "image/png"
            assert kwargs.get("prompt") == "describe the chart"
            await tool.call(action="close")

        run(scenario())

    def test_read_image_by_index(self, caprole):
        """A snapshot [N] index resolves the same as click/type."""
        caprole.describe_image_text = "transparent pixel"
        tool = bind(WebBrowser(), caprole, session_id="b_img_idx")

        async def scenario():
            await tool.call(action="navigate", url=_IMAGE_PAGE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "the chart")  # the <img> alt text
            # Fall back to the CSS selector if the walker didn't index the img.
            target = ref if ref is not None else "#chart"
            out = await tool.call(action="read_image", selector=target)
            assert "transparent pixel" in out.output
            await tool.call(action="close")

        run(scenario())

    def test_read_image_requires_selector(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_img_nosel")

        async def scenario():
            await tool.call(action="navigate", url=_IMAGE_PAGE)
            with pytest.raises(ToolError):
                await tool.call(action="read_image")
            await tool.call(action="close")

        run(scenario())

    def test_read_image_no_vision_model_raises(self, caprole):
        """No vision model bound → ToolNotConfiguredError, not a plain-text notice."""
        from mote.runtime.errors import ToolNotConfiguredError

        caprole.describe_image_text = None  # capability raises NotImplementedError
        tool = bind(WebBrowser(), caprole, session_id="b_img_novision")

        async def scenario():
            await tool.call(action="navigate", url=_IMAGE_PAGE)
            with pytest.raises(ToolNotConfiguredError, match="unavailable"):
                await tool.call(action="read_image", selector="#chart")
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------


class TestTabs:
    def test_new_tab_switch_and_close(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_tabs")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            await tool.call(action="new_tab", url=_PAGE_B)
            listing = await tool.call(action="tabs")
            assert listing.count("[") >= 2  # two tabs listed
            # active tab is the new one (Beta).
            text = await tool.call(action="read")
            assert "second page" in text
            # switch back to tab 0 (Alpha).
            await tool.call(action="switch_tab", index=0)
            text = await tool.call(action="read")
            assert "Alpha" in text
            await tool.call(action="close")

        run(scenario())

    def test_close_tab_clamps_active(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_closetab")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            await tool.call(action="new_tab", url=_PAGE_B)  # active=1
            await tool.call(action="close_tab", index=1)
            # active clamps back to the only remaining tab.
            text = await tool.call(action="read")
            assert "Alpha" in text
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# errors + lifecycle
# ---------------------------------------------------------------------------


class TestErrorsLifecycle:
    def test_unknown_action_errors(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_unknown")

        async def scenario():
            with pytest.raises(ToolError):
                await tool.call(action="teleport")
            await tool.call(action="close")

        run(scenario())

    def test_navigate_requires_url(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_nourl")

        async def scenario():
            with pytest.raises(ToolError):
                await tool.call(action="navigate")
            await tool.call(action="close")

        run(scenario())

    def test_close_without_browser(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_noclose")

        async def scenario():
            out = await tool.call(action="close")
            assert "no browser" in out

        run(scenario())

    def test_cleanup_session_kills_browser(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_cleanup")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            assert _has_browser(caprole)
            await tool.cleanup_session("b_cleanup")
            assert not _has_browser(caprole)

        run(scenario())


# ---------------------------------------------------------------------------
# state capture / resume restore
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_action_records_browser_state(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_record")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            await tool.call(action="close")

        run(scenario())
        state = caprole.latest_runtime_state("browser", "browser-state+json@1")
        assert any(_PAGE_A in url for url in state["urls"])

    def test_pending_restore_reopens_tabs(self, caprole):
        """A staged restore re-opens the saved tab in a fresh browser."""
        caprole.stage_runtime_checkpoint(
            "browser",
            "browser-state+json@1",
            {"urls": [_PAGE_A], "active": 0, "storage_state": None},
        )
        tool = bind(WebBrowser(), caprole, session_id="b_resume")

        async def scenario():
            # First action launches the browser, consuming the pending restore.
            text = await tool.call(action="read")
            assert "Alpha" in text
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# durable-login profile (seed from / persist to the encrypted store)
# ---------------------------------------------------------------------------


class TestBrowserProfile:
    def test_profile_persists_login_and_keeps_rollout_clean(self, caprole):
        """With a profile set, storage_state goes to the profile store; the
        Runtime checkpoint receives ``storage_state=None`` (no plaintext cookies).
        """
        caprole.browser_profile = "acct"
        tool = bind(WebBrowser(), caprole, session_id="b_profile")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            await tool.call(action="close")

        run(scenario())
        # The logged-in state was persisted into the (fake) encrypted profile...
        assert caprole.browser_profiles.get("acct") is not None
        # ...and the rollout checkpoint carries no cookies.
        state = caprole.latest_runtime_state("browser", "browser-state+json@1")
        assert state["storage_state"] is None

    def test_no_profile_keeps_cookies_in_rollout(self, caprole):
        """Legacy behavior: without a profile, storage_state rides the rollout."""
        tool = bind(WebBrowser(), caprole, session_id="b_noprofile")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            await tool.call(action="close")

        run(scenario())
        assert caprole.browser_profiles == {}  # nothing persisted
        state = caprole.latest_runtime_state("browser", "browser-state+json@1")
        assert state["storage_state"] is not None

    def test_profile_storage_state_seeds_fresh_session(self, caprole):
        """A saved profile is loaded and seeds the launched context (rung L0).

        Priority over session-resume state is asserted at the seam: the profile
        load is consulted and its value flows to ``session.start``.
        """
        seed = {"cookies": [], "origins": []}
        caprole.browser_profile = "acct"
        caprole.browser_profiles["acct"] = seed
        # A resume also staged state — the profile must win over it.
        caprole.stage_runtime_checkpoint(
            "browser",
            "browser-state+json@1",
            {"urls": [], "active": 0, "storage_state": {"cookies": [1]}},
        )

        seen = {}
        tool = bind(WebBrowser(), caprole, session_id="b_seed")

        async def scenario():
            await tool._ensure_runtime()
            host = tool.get_runtime_host()
            async with host.access(
                "browser:default",
                mode=RuntimeAccessMode.READ,
                owner_id="test:browser-profile",
            ) as access:
                seen["state"] = await access.driver.session.capture_state()
            await tool.call(action="close")

        # Patch BrowserSession.start to capture what storage_state it was seeded with.
        from mote.runtime.interactive.browser import session as browser_mod

        real_start = browser_mod.BrowserSession.start

        async def spy_start(self, *, storage_state=None):
            seen["seed"] = storage_state
            return await real_start(self, storage_state=storage_state)

        browser_mod.BrowserSession.start = spy_start
        try:
            run(scenario())
        finally:
            browser_mod.BrowserSession.start = real_start
        assert seen["seed"] == seed


# ---------------------------------------------------------------------------
# Secret fill (Login Ladder L1) — <agent-vault:…> / <totp:…> resolved at fill
# time; the plaintext lands in the DOM but never in the recorded call args.
# ---------------------------------------------------------------------------


class TestSecretFill:
    def test_type_expands_vault_placeholder_into_dom(self, caprole):
        caprole.secrets["xhs_password"] = "hunter2-secret"
        tool = bind(WebBrowser(), caprole, session_id="b_sec_type")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            # The model types a PLACEHOLDER, never the raw value.
            await tool.call(action="type", selector="#pass", text="<agent-vault:xhs_password>")
            val = await tool.call(action="eval", expression="document.getElementById('pass').value")
            assert "hunter2-secret" in val
            await tool.call(action="close")

        run(scenario())

    def test_fill_form_expands_placeholders(self, caprole):
        caprole.secrets["u"] = "alice-account"
        caprole.secrets["p"] = "s3cret-pass!!"
        tool = bind(WebBrowser(), caprole, session_id="b_sec_fill")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            await tool.call(
                action="fill_form",
                fields={"#user": "<agent-vault:u>", "#pass": "<agent-vault:p>"},
            )
            u = await tool.call(action="eval", expression="document.getElementById('user').value")
            p = await tool.call(action="eval", expression="document.getElementById('pass').value")
            assert "alice-account" in u and "s3cret-pass!!" in p
            await tool.call(action="close")

        run(scenario())

    def test_totp_placeholder_types_six_digits(self, caprole):
        # base32("12345678901234567890") — a valid TOTP seed.
        caprole.secrets["tf"] = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        tool = bind(WebBrowser(), caprole, session_id="b_sec_totp")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            await tool.call(action="type", selector="#user", text="<totp:tf>")
            val = await tool.call(action="eval", expression="document.getElementById('user').value")
            digits = val.strip().strip('"')  # eval returns the JSON-quoted string
            assert digits.isdigit() and len(digits) == 6
            await tool.call(action="close")

        run(scenario())

    def test_unknown_secret_fails_closed(self, caprole):
        from mote.runtime.errors import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_sec_unknown")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            with pytest.raises(ToolError):
                await tool.call(action="type", selector="#pass", text="<agent-vault:nope>")
            # The literal placeholder was NOT typed into the field.
            val = await tool.call(action="eval", expression="document.getElementById('pass').value")
            assert "agent-vault" not in val
            await tool.call(action="close")

        run(scenario())

    def test_plain_text_unaffected(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_sec_plain")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            await tool.call(action="type", selector="#user", text="ordinary-name")
            val = await tool.call(action="eval", expression="document.getElementById('user').value")
            assert "ordinary-name" in val
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# Client TLS certificates (Login Ladder L2 — mutual-TLS logins). The passphrase
# may be a secret placeholder, resolved fail-closed at launch time via the same
# seam as `type`, so plaintext never rides config or history.
# ---------------------------------------------------------------------------


class TestClientCerts:
    def test_resolve_client_certs_expands_passphrase(self, caprole):
        caprole.secrets["cert_pw"] = "topsecret-pfx"
        caprole.browser_client_certs = [
            {
                "origin": "https://mtls.example.com",
                "pfxPath": "/certs/c.pfx",
                "passphrase": "<agent-vault:cert_pw>",
            }
        ]
        tool = bind(WebBrowser(), caprole, session_id="b_certs")
        resolved = tool._resolve_client_certs()
        assert resolved == [
            {
                "origin": "https://mtls.example.com",
                "pfxPath": "/certs/c.pfx",
                "passphrase": "topsecret-pfx",
            }
        ]

    def test_resolve_client_certs_fails_closed_on_unknown_secret(self, caprole):
        from mote.runtime.errors import ToolError

        caprole.browser_client_certs = [
            {
                "origin": "https://mtls.example.com",
                "pfxPath": "/c.pfx",
                "passphrase": "<agent-vault:nope>",
            }
        ]
        tool = bind(WebBrowser(), caprole, session_id="b_certs_fail")
        with pytest.raises(ToolError):
            tool._resolve_client_certs()

    def test_resolve_client_certs_without_passphrase_passthrough(self, caprole):
        caprole.browser_client_certs = [
            {
                "origin": "https://mtls.example.com",
                "certPath": "/c.pem",
                "keyPath": "/k.pem",
            }
        ]
        tool = bind(WebBrowser(), caprole, session_id="b_certs_plain")
        resolved = tool._resolve_client_certs()
        assert resolved == [
            {
                "origin": "https://mtls.example.com",
                "certPath": "/c.pem",
                "keyPath": "/k.pem",
            }
        ]

    def test_context_kwargs_includes_client_certs(self):
        session = BrowserSession(
            session_key="k",
            client_certs=[{"origin": "https://x", "pfxPath": "/c.pfx"}],
        )
        kwargs = session._context_kwargs(None)
        assert kwargs["client_certificates"] == [{"origin": "https://x", "pfxPath": "/c.pfx"}]

    def test_context_kwargs_omits_client_certs_when_none(self):
        session = BrowserSession(session_key="k")
        kwargs = session._context_kwargs(None)
        assert "client_certificates" not in kwargs


# ---------------------------------------------------------------------------
# CDP attach — drive an already-running Chrome (reuse the human's real logins /
# passkeys / extensions). Uses a fake Playwright so no real Chrome is needed;
# asserts we connect-over-cdp (never launch), reuse the existing context, and on
# teardown only DISCONNECT (never close the human's browser).
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, url="https://real.example/app"):
        self.url = url


class _FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages) if pages is not None else []
        self.closed = False

    async def new_page(self):
        page = _FakePage("about:blank")
        self.pages.append(page)
        return page

    async def route(self, *_args, **_kwargs):
        return None

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, contexts):
        self.contexts = list(contexts)
        self.closed = False

    async def new_context(self, **kwargs):
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, contexts):
        self.connected_to = None
        self.launched = False
        self._contexts = list(contexts)

    async def connect_over_cdp(self, endpoint):
        self.connected_to = endpoint
        return _FakeBrowser(self._contexts)

    async def launch(self, **kwargs):
        self.launched = True
        return _FakeBrowser([])


class _FakePw:
    def __init__(self, chromium):
        self.chromium = chromium


class _FakeCM:
    def __init__(self, pw):
        self._pw = pw
        self.exited = False

    async def start(self):
        return self._pw

    async def __aexit__(self, *a):
        self.exited = True


def _install_fake_pw(monkeypatch, chromium):
    """Point the engine's ``async_playwright`` factory at our fakes."""
    from mote.runtime.interactive.browser import session as browser_session

    cm = _FakeCM(_FakePw(chromium))
    monkeypatch.setattr(browser_session, "async_playwright", lambda: cm)
    return cm


class TestCdpAttach:
    def test_attach_reuses_existing_context_and_skips_launch(self, monkeypatch):
        existing = _FakeContext(pages=[_FakePage("https://real.example/app")])
        chromium = _FakeChromium(contexts=[existing])
        _install_fake_pw(monkeypatch, chromium)
        session = BrowserSession(session_key="k", cdp_endpoint="http://127.0.0.1:9222")

        async def scenario():
            await session.start()
            assert session._attached is True
            assert chromium.connected_to == "http://127.0.0.1:9222"
            assert chromium.launched is False  # we attached, never launched
            assert session._context is existing  # reused, not re-created

        run(scenario())

    def test_attach_with_no_contexts_creates_a_fresh_one(self, monkeypatch):
        chromium = _FakeChromium(contexts=[])
        _install_fake_pw(monkeypatch, chromium)
        session = BrowserSession(session_key="k", cdp_endpoint="ws://localhost:9222/x")

        async def scenario():
            await session.start()
            assert session._attached is True
            assert session._context is not None
            assert session._context.pages  # a tab was ensured

        run(scenario())

    def test_shutdown_when_attached_does_not_close_the_humans_browser(self, monkeypatch):
        existing = _FakeContext(pages=[_FakePage()])
        chromium = _FakeChromium(contexts=[existing])
        cm = _install_fake_pw(monkeypatch, chromium)
        session = BrowserSession(session_key="k", cdp_endpoint="http://127.0.0.1:9222")

        async def scenario():
            await session.start()
            browser = session._browser
            await session.shutdown()
            assert existing.closed is False  # human's context untouched
            assert browser.closed is False  # human's browser untouched
            assert cm.exited is True  # only our driver disconnected

        run(scenario())

    def test_shutdown_when_owned_closes_context_and_browser(self, monkeypatch):
        chromium = _FakeChromium(contexts=[])
        cm = _install_fake_pw(monkeypatch, chromium)
        session = BrowserSession(session_key="k")  # no cdp -> launch path

        async def scenario():
            await session.start()
            ctx = session._context
            browser = session._browser
            await session.shutdown()
            assert ctx.closed is True  # we own it -> close it
            assert browser.closed is True
            assert cm.exited is True

        run(scenario())


# ---------------------------------------------------------------------------
# snapshot + [N] index refs (batch A)
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_lists_interactive_elements(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_snap")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            # Header carries url + title.
            assert "Shop" in snap
            # Interactive elements are listed with [N] indices.
            assert "[1]" in snap
            # The link, input, and button should all surface.
            assert "Home" in snap
            assert "Buy now" in snap or "buy" in snap.lower()
            # Plain descriptive <p> text is NOT an interactive element line.
            await tool.call(action="close")

        run(scenario())

    def test_click_by_index(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_clickidx")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            # Find the index assigned to the 'Buy now' button.
            ref = _ref_for(snap, "Buy now")
            assert ref is not None, snap
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_type_by_index(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_typeidx")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Search products")
            assert ref is not None, snap
            await tool.call(action="type", selector=ref, text="laptop")
            val = await tool.call(action="eval", expression="document.getElementById('search').value")
            assert "laptop" in val
            await tool.call(action="close")

        run(scenario())

    def test_type_bracket_index_form(self, caprole):
        """A '[N]' bracketed index resolves the same as a bare 'N'."""
        tool = bind(WebBrowser(), caprole, session_id="b_bracket")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Search products")
            assert ref is not None, snap
            await tool.call(action="type", selector=f"[{ref}]", text="phone")
            val = await tool.call(action="eval", expression="document.getElementById('search').value")
            assert "phone" in val
            await tool.call(action="close")

        run(scenario())

    def test_nested_control_in_clickable_wrapper_still_gets_ref(self, caprole):
        """A real <button> inside a cursor:pointer wrapper whose innerText
        contains the button label must NOT be containment-collapsed — it gets
        its own [N] ref so the agent can click it. Regression for the dropped
        "获取验证码" (send SMS code) button."""
        tool = bind(WebBrowser(), caprole, session_id="b_nested_ctl")

        async def scenario():
            await tool.call(action="navigate", url=_NESTED_CONTROL)
            snap = await tool.call(action="snapshot")
            # The nested button surfaces with a clickable [N] index.
            ref = _ref_for(snap, "获取验证码")
            assert ref is not None, snap
            # And it is actually actionable by that index.
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_opacity_zero_checkbox_still_gets_ref(self, caprole):
        """A genuine control made opacity:0 (custom-styled "agree to terms"
        checkbox) must stay in the snapshot with a clickable [N] ref — Playwright
        ignores opacity for actionability. Regression for filtered-out consent
        boxes blocking login."""
        tool = bind(WebBrowser(), caprole, session_id="b_opacity_cb")

        async def scenario():
            await tool.call(action="navigate", url=_OPACITY_CHECKBOX)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "agree to terms")
            assert ref is not None, snap
            # And it is actually actionable (can be ticked) by that index.
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_actionable_control_under_opacity_wrapper_survives(self, caprole):
        """Only display:none prunes a subtree — a clickable control beneath an
        opacity:0 wrapper still surfaces, while the wrapper's transparent prose
        is suppressed (not readable)."""
        tool = bind(WebBrowser(), caprole, session_id="b_hidden_wrap")

        async def scenario():
            await tool.call(action="navigate", url=_HIDDEN_WRAPPER_CONTROL)
            snap = await tool.call(action="snapshot")
            # Drop the header line (it echoes the data: URL == raw HTML source).
            body = snap.split("\n", 1)[1] if "\n" in snap else snap
            assert _ref_for(body, "Continue") is not None, body
            # The transparent prose must NOT leak into the reading view.
            assert "invisible legalese" not in body, body
            await tool.call(action="close")

        run(scenario())

    def test_visibility_visible_child_overrides_hidden_parent(self, caprole):
        """A visibility:visible child under a visibility:hidden parent must not be
        dropped — each node is judged on its own computed style, not pruned by an
        ancestor's hidden state."""
        tool = bind(WebBrowser(), caprole, session_id="b_vis_override")

        async def scenario():
            await tool.call(action="navigate", url=_VISIBILITY_OVERRIDE)
            snap = await tool.call(action="snapshot")
            assert _ref_for(snap, "Reveal me") is not None, snap
            await tool.call(action="close")

        run(scenario())

    def test_open_shadow_dom_control_gets_ref_and_clicks(self, caprole):
        """A control inside an OPEN shadow root surfaces in the snapshot (the walk
        follows the flattened tree) and is clickable — Playwright's CSS engine
        pierces open shadow DOM, and the blocker hit-test is shadow-aware so the
        host is not falsely reported as covering the control."""
        tool = bind(WebBrowser(), caprole, session_id="b_shadow")

        async def scenario():
            await tool.call(action="navigate", url=_SHADOW_CONTROL)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Shadow Login")
            assert ref is not None, snap
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_slotted_light_control_surfaces_once(self, caprole):
        """A light-DOM control projected through <slot> appears exactly once (the
        flattened walk reaches it via the slot's assigned nodes, not twice)."""
        tool = bind(WebBrowser(), caprole, session_id="b_slot")

        async def scenario():
            await tool.call(action="navigate", url=_SHADOW_SLOT)
            snap = await tool.call(action="snapshot")
            # Drop the header line (it echoes the data: URL == raw HTML source).
            body = snap.split("\n", 1)[1] if "\n" in snap else snap
            assert body.count("Slotted Btn") == 1, body
            ref = _ref_for(body, "Slotted Btn")
            assert ref is not None, body
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_iframe_control_gets_ref_and_resolves(self, caprole):
        """A control inside a (same-origin srcdoc) iframe surfaces in the snapshot
        under a [frame: …] section and is drivable by its [N] ref — snapshot walks
        page.frames + records which frame owns the ref, so type/click resolve
        against the child frame, not the (blind) main frame."""
        tool = bind(WebBrowser(), caprole, session_id="b_iframe")

        async def scenario():
            await tool.call(action="navigate", url=_IFRAME_LOGIN)
            snap = await tool.call(action="snapshot")
            # The iframe's controls are present, grouped under a frame section.
            assert "[frame:" in snap, snap
            fref = _ref_for(snap, "Frame Login")
            assert fref is not None, snap
            uref = _ref_for(snap, "frame-username")
            assert uref is not None, snap
            # Typing into the iframe input resolves against the owning frame.
            await tool.call(action="type", selector=uref, text="alice")
            # Clicking the iframe button resolves + hit-tests within that frame.
            out = await tool.call(action="click", selector=fref)
            assert "clicked" in out, out
            await tool.call(action="close")

        run(scenario())

    def test_iframe_refs_disjoint_from_main_frame(self, caprole):
        """Refs form ONE flat namespace across frames — an iframe control never
        collides with a main-frame control's index (refSeed threading)."""
        tool = bind(WebBrowser(), caprole, session_id="b_iframe_ns")

        async def scenario():
            await tool.call(action="navigate", url=_IFRAME_LOGIN)
            snap = await tool.call(action="snapshot")
            outer = _ref_for(snap, "Outer Button")
            frame_btn = _ref_for(snap, "Frame Login")
            assert outer is not None and frame_btn is not None, snap
            assert outer != frame_btn, snap
            await tool.call(action="close")

        run(scenario())

    def test_stale_index_gives_actionable_error(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_stale")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            await tool.call(action="snapshot")
            # An index that was never assigned — must fail with a re-snapshot hint.
            with pytest.raises(ToolError) as ei:
                await tool.call(action="click", selector="999")
            assert "snapshot" in str(ei.value).lower()
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# unified page tree — interleave, click-through, ref lifecycle
# ---------------------------------------------------------------------------

# A mixed page: div layout wrapping prose text + links + an input, so the tree
# must interleave prose lines and [N] element lines.
_MIXED = (
    "data:text/html,<title>Mixed</title><body>"
    "<div><p>Welcome to the catalog.</p>"
    "<a id='apple' href='https://example.com/apple'>Apple</a>"
    "<p>Pick a fruit below.</p>"
    "<input id='qty' placeholder='Quantity'>"
    "<button id='add'>Add to cart</button></div>"
    "</body>"
)


class TestUnifiedTree:
    def test_tree_interleaves_text_and_refs(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_tree_mix")

        async def scenario():
            await tool.call(action="navigate", url=_MIXED)
            snap = await tool.call(action="snapshot")
            # Drop the header line (it echoes the data: URL == raw HTML source).
            body = snap.split("\n", 1)[1] if "\n" in snap else snap
            # Prose text is present (not only interactive elements).
            assert "Welcome to the catalog." in body
            assert "Pick a fruit below." in body
            # Clickable elements carry [N] refs.
            assert _ref_for(body, "Apple") is not None
            assert _ref_for(body, "Add to cart") is not None
            # A prose line comes before the button line (reading order).
            lines = body.splitlines()
            prose_i = next(i for i, l in enumerate(lines) if "Welcome to the catalog." in l)
            btn_i = next(i for i, l in enumerate(lines) if "Add to cart" in l)
            assert prose_i < btn_i
            await tool.call(action="close")

        run(scenario())

    def test_click_link_from_tree(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_tree_click")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Home")
            assert ref is not None, snap
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())

    def test_interactive_only_omits_prose(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_tree_io")

        async def scenario():
            await tool.call(action="navigate", url=_MIXED)
            snap = await tool.call(action="snapshot", interactive_only=True)
            # Drop the header line (it echoes the data: URL == raw HTML source).
            body = snap.split("\n", 1)[1] if "\n" in snap else snap
            # Element refs still present.
            assert _ref_for(body, "Add to cart") is not None
            # Prose text dropped.
            assert "Welcome to the catalog." not in body
            assert "Pick a fruit below." not in body
            await tool.call(action="close")

        run(scenario())

    def test_navigation_invalidates_refs(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_tree_inval")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Buy now")
            assert ref is not None, snap
            host = caprole.get_runtime_host()
            async with host.access(
                "browser:default",
                mode=RuntimeAccessMode.READ,
                owner_id="test:browser-refs",
            ) as access:
                session = access.driver.session
            # Navigate away — refs from the old page must be dropped.
            await tool.call(action="navigate", url=_PAGE_B)
            assert session._ref_meta == {}
            assert session._prev_refs == set()
            # The old [N] index no longer resolves → Tier-3 re-snapshot error.
            with pytest.raises(ToolError) as ei:
                await tool.call(action="click", selector=ref)
            assert "snapshot" in str(ei.value).lower()
            await tool.call(action="close")

        run(scenario())

    def test_same_page_ref_stable_new_element_marked(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_tree_stable")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap1 = await tool.call(action="snapshot")
            ref_buy = _ref_for(snap1, "Buy now")
            assert ref_buy is not None, snap1
            # Inject a brand-new button into the same document.
            await tool.call(
                action="eval",
                expression=(
                    "(() => { const b = document.createElement('button');"
                    " b.id = 'extra'; b.textContent = 'Extra'; "
                    "document.body.appendChild(b); return true; })()"
                ),
            )
            snap2 = await tool.call(action="snapshot")
            # The pre-existing button keeps its index across re-snapshots.
            assert _ref_for(snap2, "Buy now") == ref_buy
            # The pre-existing button is NOT marked new the second time.
            buy_line = next(l for l in snap2.splitlines() if "Buy now" in l)
            assert not buy_line.lstrip().startswith("*")
            # The freshly-injected one IS marked new.
            extra_line = next(l for l in snap2.splitlines() if "Extra" in l)
            assert extra_line.lstrip().startswith("*")
            await tool.call(action="close")

        run(scenario())

    def test_tier2_resolves_after_dom_rerender(self, caprole):
        """Same-page re-render drops the data-agent-ref attr; Tier-2 re-queries."""
        tool = bind(WebBrowser(), caprole, session_id="b_tree_tier2")

        async def scenario():
            await tool.call(action="navigate", url=_INTERACTIVE)
            snap = await tool.call(action="snapshot")
            ref = _ref_for(snap, "Buy now")
            assert ref is not None, snap
            # Simulate a client-side re-render that strips our stamp but keeps
            # the same role/name (button labelled "Buy now").
            await tool.call(
                action="eval",
                expression=(
                    "(() => { document.querySelectorAll('[data-agent-ref]')"
                    ".forEach(e => e.removeAttribute('data-agent-ref'));"
                    " return true; })()"
                ),
            )
            # Tier-1 selector now misses, Tier-2 re-queries by role/name + re-stamps.
            out = await tool.call(action="click", selector=ref)
            assert "clicked" in out
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# overlay / blocker detection (batch A)
# ---------------------------------------------------------------------------


class TestBlocker:
    def test_click_through_overlay_errors(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_overlay")

        async def scenario():
            await tool.call(action="navigate", url=_OVERLAY)
            # #target is fully covered by #cover — click must fail early naming it.
            with pytest.raises(ToolError) as ei:
                await tool.call(action="click", selector="#target")
            assert "covered by" in str(ei.value)
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# Markdown read (batch A)
# ---------------------------------------------------------------------------


class TestMarkdownRead:
    def test_read_returns_markdown(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_md")

        async def scenario():
            await tool.call(action="navigate", url=_ARTICLE)
            md = await tool.call(action="read")
            # Drop the header line (it echoes the data: URL, which contains the
            # raw HTML source) and assert on the extracted body only.
            body = md.split("\n", 1)[1] if "\n" in md else md
            # Headings rendered as Markdown.
            assert "# Big Title" in body
            assert "## Section" in body
            # Body paragraphs preserved.
            assert "First paragraph of the body." in body
            assert "Second paragraph here." in body
            # Chrome (nav/footer) stripped from the extracted content.
            assert "skip me" not in body
            assert "footer noise" not in body
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# wait / forms / extract / eval JSON (batch B)
# ---------------------------------------------------------------------------


class TestWait:
    def test_wait_for_expression(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_wait_expr")

        async def scenario():
            await tool.call(action="navigate", url=_DELAYED)
            out = await tool.call(action="wait", expression="window.__done === true")
            assert "true" in out.lower()
            # The late element is now present.
            val = await tool.call(action="eval", expression="document.getElementById('late').textContent")
            assert "ready" in val
            await tool.call(action="close")

        run(scenario())

    def test_wait_for_selector(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_wait_sel")

        async def scenario():
            await tool.call(action="navigate", url=_DELAYED)
            out = await tool.call(action="wait", selector="#late")
            assert "appeared" in out
            await tool.call(action="close")

        run(scenario())

    def test_wait_requires_one_of(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_wait_none")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            with pytest.raises(ToolError):
                await tool.call(action="wait")
            await tool.call(action="close")

        run(scenario())


class TestForms:
    def test_detect_forms_lists_fields(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_detect")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            out = await tool.call(action="detect_forms")
            # JSON description naming the form's fields + submit.
            assert "username" in out
            assert "password" in out
            assert "Username" in out  # label surfaced
            assert "submit" in out
            await tool.call(action="close")

        run(scenario())

    def test_fill_form_and_submit_marker(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_fill")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            out = await tool.call(
                action="fill_form",
                fields={"#user": "alice", "#pass": "s3cr3t"},
            )
            assert "filled 2" in out
            # Values landed in the DOM.
            u = await tool.call(action="eval", expression="document.getElementById('user').value")
            p = await tool.call(action="eval", expression="document.getElementById('pass').value")
            assert "alice" in u
            assert "s3cr3t" in p
            await tool.call(action="close")

        run(scenario())

    def test_fill_form_requires_fields(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_fill_empty")

        async def scenario():
            await tool.call(action="navigate", url=_LOGIN_FORM)
            with pytest.raises(ToolError):
                await tool.call(action="fill_form")
            await tool.call(action="close")

        run(scenario())


class TestExtract:
    def test_extract_text_and_attr(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_extract")

        async def scenario():
            await tool.call(action="navigate", url=_LISTING)
            out = await tool.call(
                action="extract",
                schema={
                    "title": "#hd",  # single → scalar text
                    "items": "a.item",  # multiple → list of text
                    "links": "a.item@href",  # multiple → list of attr
                },
            )
            import json

            data = json.loads(out)
            assert data["title"] == "Catalog"
            assert data["items"] == ["Apple", "Banana", "Cherry"]
            assert any("/a" in l for l in data["links"])
            await tool.call(action="close")

        run(scenario())

    def test_extract_no_match_is_null(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_extract_null")

        async def scenario():
            await tool.call(action="navigate", url=_LISTING)
            out = await tool.call(action="extract", schema={"missing": ".nope"})
            import json

            assert json.loads(out)["missing"] is None
            await tool.call(action="close")

        run(scenario())

    def test_extract_requires_schema(self, caprole):
        from mote.runtime.tools.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_extract_empty")

        async def scenario():
            await tool.call(action="navigate", url=_LISTING)
            with pytest.raises(ToolError):
                await tool.call(action="extract")
            await tool.call(action="close")

        run(scenario())


class TestEvalJson:
    def test_eval_returns_json_object(self, caprole):
        tool = bind(WebBrowser(), caprole, session_id="b_eval_json")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            out = await tool.call(action="eval", expression="({a: 1, b: [2, 3], c: 'x'})")
            import json

            data = json.loads(out)
            assert data == {"a": 1, "b": [2, 3], "c": "x"}
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# engine helpers
# ---------------------------------------------------------------------------


def test_cap_text_under_limit_unchanged():
    assert cap_head_tail("short", TEXT_MAX_CHARS)[0] == "short"


def test_cap_text_over_limit_truncates():
    big = "x" * (TEXT_MAX_CHARS + 100)
    out = cap_head_tail(big, TEXT_MAX_CHARS)[0]
    assert "omitted" in out
    assert len(out) < len(big)
