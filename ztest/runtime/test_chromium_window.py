from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.notebook import NOTEBOOK_MEDIA_TYPE, NotebookCell, NotebookDocument
from mote.contracts.surfaces import SurfaceDescriptor, SurfaceFrame
from mote.contracts.terminal import TERMINAL_MEDIA_TYPE
from mote.runtime.interactive import chromium_window as window_module
from mote.runtime.interactive.chromium_window import ChromiumLiveWindowBackend


class _Page:
    def __init__(self) -> None:
        self.binding = None
        self.close_callback = None
        self.evaluations = []
        self.content = ""
        self.scripts = []
        self._closed = False

    def is_closed(self):
        return self._closed

    def once(self, event, callback):
        assert event == "close"
        self.close_callback = callback

    async def expose_binding(self, name, binding):
        assert name == "__moteInput"
        self.binding = binding

    async def set_content(self, content):
        self.content = content

    async def add_script_tag(self, *, content):
        self.scripts.append(content)

    async def evaluate(self, expression, value):
        self.evaluations.append((expression, value))

    def close_from_user(self):
        self._closed = True
        self.close_callback()


class _Context:
    def __init__(self, page) -> None:
        self.page = page

    async def new_page(self):
        self.page.context = self
        return self.page


class _Browser:
    def __init__(self, page) -> None:
        self.page = page
        self.connected = True
        self.context_kwargs = None

    def is_connected(self):
        return self.connected

    def on(self, event, callback):
        assert event == "disconnected"
        self.disconnect_callback = callback

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return _Context(self.page)

    async def close(self):
        self.connected = False


class _Chromium:
    def __init__(self, browser) -> None:
        self.browser = browser
        self.launch_kwargs = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser


class _Playwright:
    def __init__(self, chromium) -> None:
        self.chromium = chromium


class _Manager:
    def __init__(self, playwright) -> None:
        self.playwright = playwright
        self.closed = False

    async def start(self):
        return self.playwright

    async def __aexit__(self, *_args):
        self.closed = True


@pytest.mark.asyncio
async def test_chromium_viewer_fences_input_without_real_gui(monkeypatch):
    page = _Page()
    browser = _Browser(page)
    chromium = _Chromium(browser)
    manager = _Manager(_Playwright(chromium))
    monkeypatch.setattr(window_module, "async_playwright", lambda: manager)

    async def _focus(_page):
        return None

    monkeypatch.setattr(window_module, "focus_chromium_page", _focus)
    frame = SurfaceFrame(
        sequence=1,
        media_type="application/vnd.mote.browser+json",
        content=json.dumps({"tabs": "about:blank", "screenshot_b64": "ZmFrZQ=="}),
    )
    session = await ChromiumLiveWindowBackend().open(
        SurfaceDescriptor(kind="browser", ref="browser:test", title="Browser"),
        frame,
    )
    received = []

    async def _receive(event):
        received.append(event)

    await session.set_input_handler(_receive)
    await page.binding(None, {"kind": "browser.text", "data": "hello"})
    assert received[0].kind == "browser.text"
    assert chromium.launch_kwargs["headless"] is False
    assert browser.context_kwargs == {"no_viewport": True}
    assert "browser.drag" in "".join(page.scripts)

    await session.set_input_handler(None)
    await page.binding(None, {"kind": "browser.text", "data": "ignored"})
    assert len(received) == 1

    closed = asyncio.create_task(session.wait_closed())
    page.close_from_user()
    await asyncio.wait_for(closed, timeout=1)
    await session.aclose()
    assert manager.closed is True


@pytest.mark.asyncio
async def test_chromium_viewer_selects_safe_notebook_frontend(monkeypatch):
    page = _Page()
    browser = _Browser(page)
    chromium = _Chromium(browser)
    manager = _Manager(_Playwright(chromium))
    monkeypatch.setattr(window_module, "async_playwright", lambda: manager)

    async def _focus(_page):
        return None

    monkeypatch.setattr(window_module, "focus_chromium_page", _focus)
    document = NotebookDocument(
        ref="jupyter-notebook:test",
        cells=[NotebookCell(id="cell-agent", source="2 + 2")],
    )
    session = await ChromiumLiveWindowBackend().open(
        SurfaceDescriptor(kind="notebook", ref=document.ref, title="Jupyter Notebook"),
        SurfaceFrame(
            sequence=1,
            media_type=NOTEBOOK_MEDIA_TYPE,
            content=document.model_dump_json(),
        ),
    )
    received = []

    async def _receive(event):
        received.append(event)

    assert 'id="notebook"' in page.content
    scripts = "".join(page.scripts)
    assert "notebook.execute" in scripts
    assert "innerHTML" not in page.content + scripts
    await session.set_input_handler(_receive)
    await page.binding(
        None,
        {
            "kind": "notebook.execute",
            "data": json.dumps({"cell_id": "cell-human", "source": "3 + 3"}),
        },
    )
    assert received[0].kind == "notebook.execute"
    await session.aclose()


@pytest.mark.asyncio
async def test_chromium_viewer_loads_offline_xterm_frontend(monkeypatch):
    page = _Page()
    browser = _Browser(page)
    chromium = _Chromium(browser)
    manager = _Manager(_Playwright(chromium))
    monkeypatch.setattr(window_module, "async_playwright", lambda: manager)

    async def _focus(_page):
        return None

    monkeypatch.setattr(window_module, "focus_chromium_page", _focus)
    session = await ChromiumLiveWindowBackend().open(
        SurfaceDescriptor(kind="terminal", ref="terminal:test", title="Terminal"),
        SurfaceFrame(sequence=1, media_type=TERMINAL_MEDIA_TYPE, content="hello\r\n"),
    )
    scripts = "".join(page.scripts)
    assert 'id="terminal"' in page.content
    assert "terminal.input" in scripts
    assert "terminal.resize" in scripts
    assert "FitAddon" in scripts
    await session.aclose()
