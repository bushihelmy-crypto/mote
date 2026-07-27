from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.handoff import HandoffRequest, HandoffStatus, HumanHandoffOutcome
from mote.contracts.runtimes import CheckpointFidelity, RuntimeCheckpoint, RuntimeRef
from mote.contracts.surfaces import SurfaceInput, SurfacePresentationMode
from mote.product.toolsets.builtin.web_browser import WebBrowser
from mote.runtime.interactive.checkpoint_codec import decode_inline_json, encode_inline_json
from mote.runtime.tools.dependency import _browser_runtime as browser_runtime_module
from mote.runtime.tools.dependency._browser_runtime import BrowserRuntimeDriver


class _FakeBrowserSession:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.headless = kwargs.get("headless", True)
        self.closed = False
        self.started_with = None
        self.restored = None
        self.inputs = []

    async def start(self, *, storage_state=None):
        self.started_with = storage_state

    async def restore_state(self, urls, active=0, storage_state=None):
        self.restored = (urls, active, storage_state)

    async def capture_state(self):
        return (["data:text/html,ok"], 0, {"cookies": [], "origins": []})

    async def tabs(self):
        return "* [0] data:text/html,ok"

    async def screenshot(self):
        return b"fake-png"

    async def focus(self):
        return None

    async def handoff_pointer(self, x, y):
        self.inputs.append(("pointer", x, y))

    async def handoff_drag(self, x, y, x2, y2):
        self.inputs.append(("drag", x, y, x2, y2))

    async def handoff_text(self, text):
        self.inputs.append(("text", text))

    async def handoff_key(self, key):
        self.inputs.append(("key", key))

    async def back(self):
        self.inputs.append(("back",))

    async def shutdown(self):
        self.closed = True

    def kill(self):
        self.closed = True


def test_browser_runtime_passes_a_user_owned_cdp_endpoint_to_the_session():
    driver = BrowserRuntimeDriver(
        session_key="attached-browser-test",
        cdp_endpoint="http://127.0.0.1:9222",
    )

    assert driver._session_kwargs["cdp_endpoint"] == "http://127.0.0.1:9222"


@pytest.mark.asyncio
async def test_browser_profile_persistence_is_independent_from_runtime_sink():
    saved = []
    tool = WebBrowser()
    tool.get_browser_profile = lambda: "acct"
    tool.save_browser_profile = lambda profile, state: saved.append((profile, state))
    session = _FakeBrowserSession()

    await tool._persist_profile(session)

    assert saved == [("acct", {"cookies": [], "origins": []})]


@pytest.mark.asyncio
async def test_browser_runtime_handoff_retains_observation_not_input(monkeypatch):
    monkeypatch.setattr(browser_runtime_module, "BrowserSession", _FakeBrowserSession)
    encoded = encode_inline_json(
        {"urls": ["data:text/html,ok"], "active": 0},
        codec="browser-state+json@1",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    checkpoint = RuntimeCheckpoint(
        runtime_id="browser-runtime",
        kind="browser",
        epoch=0,
        revision=0,
        codec=encoded.codec,
        schema_version=encoded.schema_version,
        payload_ref=encoded.payload_ref,
        digest=encoded.digest,
        fidelity=encoded.fidelity or CheckpointFidelity.LOGICAL,
    )
    driver = BrowserRuntimeDriver(
        session_key="browser-runtime-test",
        storage_state={"cookies": [], "origins": []},
    )
    started = await driver.start(checkpoint)
    assert started.restored is True
    assert driver.session.restored is not None
    assert driver.session.kwargs["headless"] is True

    handle = await driver.prepare_handoff(
        HandoffRequest(runtime_ref=RuntimeRef(runtime_id="browser-1", kind="browser"))
    )
    try:
        assert driver.session.headless is False
        assert handle.surface.presentation is SurfacePresentationMode.EMBEDDED
        frame = await driver.snapshot_surface(handle)
        payload = json.loads(frame.content)
        assert payload["screenshot_b64"]

        await driver.send_surface_input(handle, SurfaceInput(kind="browser.pointer", data='{"x":10,"y":20}'))
        assert ("pointer", 10.0, 20.0) in driver.session.inputs
        await driver.send_surface_input(
            handle,
            SurfaceInput(kind="browser.drag", data='{"x":10,"y":20,"x2":90,"y2":20}'),
        )
        assert ("drag", 10.0, 20.0, 90.0, 20.0) in driver.session.inputs
        sequence = (await driver.snapshot_surface(handle)).sequence
        await driver.finish_handoff(handle, HumanHandoffOutcome(status=HandoffStatus.COMPLETED))

        update = asyncio.create_task(driver.next_surface_frame(handle, sequence))
        driver.surface_changed()
        observed = await asyncio.wait_for(update, timeout=1)
        assert observed is not None
        assert observed.sequence == sequence + 1

        with pytest.raises(RuntimeError, match="handoff handle"):
            await driver.send_surface_input(handle, SurfaceInput(kind="browser.key", data="Enter"))
        await driver.detach_surface(handle)
    finally:
        await driver.aclose()

    assert driver.closed is True


@pytest.mark.asyncio
async def test_profile_checkpoint_encrypts_storage_outside_rollout(monkeypatch):
    monkeypatch.setattr(browser_runtime_module, "BrowserSession", _FakeBrowserSession)
    driver = BrowserRuntimeDriver(
        session_key="profile-checkpoint-test",
        persist_storage_state=False,
    )
    await driver.start()
    try:
        encoded = await driver.checkpoint("write-commit")
    finally:
        await driver.aclose()

    checkpoint = RuntimeCheckpoint(
        runtime_id="browser-profile",
        kind="browser",
        epoch=1,
        revision=1,
        codec=encoded.codec,
        schema_version=encoded.schema_version,
        payload_ref=encoded.payload_ref,
        digest=encoded.digest,
        fidelity=encoded.fidelity or CheckpointFidelity.LOGICAL,
    )
    payload = decode_inline_json(checkpoint, codec="browser-state+json@1")

    assert payload == {
        "urls": ["data:text/html,ok"],
        "active": 0,
        "storage_state": None,
    }
