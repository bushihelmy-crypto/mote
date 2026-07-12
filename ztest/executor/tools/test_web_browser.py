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
``CapRole`` (stored on its ``tool_sessions``, mirroring ``RoleState``), so there
is no process-global singleton to leak; each test still closes its browser to
free the subprocess.

Skipped entirely when Playwright / its Chromium browser is unavailable.
"""
from __future__ import annotations

import pytest

from metagpt.executor.dependency._browser import _cap_text
from metagpt.executor.tools.web_browser import WebBrowser

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


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="Playwright Chromium not available"
)

# A couple of offline pages.
_PAGE_A = "data:text/html,<title>Alpha</title><body><h1>Alpha</h1><p>hello world</p></body>"
_PAGE_B = "data:text/html,<title>Beta</title><body><p>second page</p></body>"
_FORM = (
    "data:text/html,<title>Form</title><body>"
    "<input id='q' value=''><button id='go'>Go</button></body>"
)

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


def _has_browser(role: CapRole) -> bool:
    return role.get_tool_session("WebBrowser") is not None


def _ref_for(snapshot: str, needle: str) -> "str | None":
    """Pull the ``[N]`` index of the first snapshot line containing *needle*.

    Returns the bare numeric ref (e.g. ``"3"``) so tests can drive click/type
    by index without hard-coding which index the walker assigns.
    """
    import re

    for line in snapshot.splitlines():
        if needle in line:
            m = re.match(r"\s*\[(\d+)\]", line)
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
            # ToolResult with a base64 image attached.
            assert result.images and isinstance(result.images[0], str)
            assert result.data["type"] == "screenshot"
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
        from metagpt.executor.tool_result import ToolError

        tool = bind(WebBrowser(), caprole, session_id="b_unknown")

        async def scenario():
            with pytest.raises(ToolError):
                await tool.call(action="teleport")
            await tool.call(action="close")

        run(scenario())

    def test_navigate_requires_url(self, caprole):
        from metagpt.executor.tool_result import ToolError

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

        run(scenario())
        tool.cleanup_session("b_cleanup")
        assert not _has_browser(caprole)


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
        # At least one browser-state capture recorded (urls + active + storage).
        assert caprole.browser_states
        urls, active, storage_state, name = caprole.browser_states[-1]
        assert any(_PAGE_A in u for u in urls)
        assert name == "WebBrowser"

    def test_pending_restore_reopens_tabs(self, caprole):
        """A staged restore re-opens the saved tab in a fresh browser."""
        caprole._pending_browser_restore = {
            "urls": [_PAGE_A],
            "active": 0,
            "storage_state": None,
        }
        tool = bind(WebBrowser(), caprole, session_id="b_resume")

        async def scenario():
            # First action launches the browser, consuming the pending restore.
            text = await tool.call(action="read")
            assert "Alpha" in text
            await tool.call(action="close")

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
            val = await tool.call(
                action="eval", expression="document.getElementById('search').value"
            )
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
            val = await tool.call(
                action="eval", expression="document.getElementById('search').value"
            )
            assert "phone" in val
            await tool.call(action="close")

        run(scenario())

    def test_stale_index_gives_actionable_error(self, caprole):
        from metagpt.executor.tool_result import ToolError

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
# overlay / blocker detection (batch A)
# ---------------------------------------------------------------------------


class TestBlocker:
    def test_click_through_overlay_errors(self, caprole):
        from metagpt.executor.tool_result import ToolError

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
            val = await tool.call(
                action="eval", expression="document.getElementById('late').textContent"
            )
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
        from metagpt.executor.tool_result import ToolError

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
            u = await tool.call(
                action="eval", expression="document.getElementById('user').value"
            )
            p = await tool.call(
                action="eval", expression="document.getElementById('pass').value"
            )
            assert "alice" in u
            assert "s3cr3t" in p
            await tool.call(action="close")

        run(scenario())

    def test_fill_form_requires_fields(self, caprole):
        from metagpt.executor.tool_result import ToolError

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
                    "title": "#hd",            # single → scalar text
                    "items": "a.item",         # multiple → list of text
                    "links": "a.item@href",    # multiple → list of attr
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
        from metagpt.executor.tool_result import ToolError

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
            out = await tool.call(
                action="eval", expression="({a: 1, b: [2, 3], c: 'x'})"
            )
            import json

            data = json.loads(out)
            assert data == {"a": 1, "b": [2, 3], "c": "x"}
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# assist — human-in-the-loop handoff (scan QR / SMS / 2FA)
# ---------------------------------------------------------------------------


class TestAssist:
    def test_headless_screenshots_and_asks_human(self, caprole):
        """Headless assist screenshots the page to disk, then asks the user.

        No visible window is needed: the engine captures a PNG, writes it under
        ``{cwd}/.agent_browser/``, and the ask_human prompt names that file so
        the user can open it, read the code/QR, and reply. We assert the file
        was written and that the prompt carries both our instruction and the
        path, then that the user's reply comes back.
        """
        import os

        caprole.ask_reply = "123456"
        caprole.browser_headless = True
        tool = bind(WebBrowser(), caprole, session_id="b_assist_headless")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            out = await tool.call(action="assist", prompt="read the one-time code")
            # The user was prompted, and the prompt carried our instruction.
            assert len(caprole.ask_questions) == 1
            asked = caprole.ask_questions[0]
            assert "read the one-time code" in asked
            assert ".agent_browser" in asked
            assert "assist_" in asked and ".png" in asked
            # The screenshot file the prompt names actually exists on disk.
            shot_dir = os.path.join(caprole.get_cwd(), ".agent_browser")
            shots = [f for f in os.listdir(shot_dir) if f.endswith(".png")]
            assert shots, "expected a screenshot PNG to be written"
            # The user's reply is surfaced so the model can act on it.
            assert "123456" in out
            await tool.call(action="close")

        run(scenario())

    def test_headed_pauses_and_asks_human(self, caprole):
        """Headed assist asks the user to act in the window, then resumes.

        The browser itself always launches headless (safe on a CI / WSL2 box
        with no display); we flip the headless *flag* to False only after the
        session exists, so ``assist`` takes the in-window handoff path without
        ever opening a real visible window.
        """
        caprole.ask_reply = "done"
        tool = bind(WebBrowser(), caprole, session_id="b_assist_headed")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)  # launches headless
            caprole.browser_headless = False  # flag-only: take the handoff path
            out = await tool.call(action="assist", prompt="scan the login QR code")
            assert len(caprole.ask_questions) == 1
            asked = caprole.ask_questions[0]
            assert "scan the login QR code" in asked
            assert "browser handoff" in asked
            assert "resumed by user" in out
            await tool.call(action="close")

        run(scenario())

    def test_missing_prompt_is_actionable_error(self, caprole):
        # Browser stays headless (default) — the empty-prompt guard fires in
        # _dispatch before any screenshot/ask, so no work is done.
        tool = bind(WebBrowser(), caprole, session_id="b_assist_noprompt")

        async def scenario():
            await tool.call(action="navigate", url=_PAGE_A)
            with pytest.raises(Exception) as ei:
                await tool.call(action="assist")
            assert "requires a 'prompt'" in str(ei.value)
            await tool.call(action="close")

        run(scenario())


# ---------------------------------------------------------------------------
# engine helpers
# ---------------------------------------------------------------------------


def test_cap_text_under_limit_unchanged():
    assert _cap_text("short") == "short"


def test_cap_text_over_limit_truncates():
    from metagpt.executor.dependency._browser import TEXT_MAX_CHARS

    big = "x" * (TEXT_MAX_CHARS + 100)
    out = _cap_text(big)
    assert "omitted" in out
    assert len(out) < len(big)
