"""Tests for DeviceSession: observe → snapshot, ref resolution, stale detection, lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from mote.runtime.interactive.device.backend import DeviceError
from mote.runtime.interactive.device.session import DeviceSession
from mote.ztest.executor.dependency.device_fakes import FAKE_PNG, FakeDeviceBackend


def _run(coro):
    return asyncio.run(coro)


def _session(**kw) -> DeviceSession:
    return DeviceSession(session_key="DeviceUse", backend=FakeDeviceBackend(**kw))


def test_observe_fused_returns_text_and_screenshot():
    sess = _session()
    obs = _run(sess.observe(mode="fused"))
    assert obs.screenshot == FAKE_PNG
    assert "@e" in obs.text  # interactive refs rendered
    assert obs.width == 1080 and obs.height == 2340
    assert obs.empty is False
    assert obs.state_id == "s1"


def test_observe_semantic_omits_screenshot():
    sess = _session()
    obs = _run(sess.observe(mode="semantic"))
    assert obs.screenshot is None
    assert obs.text


def test_observe_visual_omits_outline():
    sess = _session()
    obs = _run(sess.observe(mode="visual"))
    assert obs.screenshot == FAKE_PNG
    assert obs.text == ""
    assert obs.empty is True
    assert obs.width == 1080


def test_capture_screen_does_not_mutate_semantic_snapshot():
    sess = _session()
    observed = _run(sess.observe(mode="semantic"))

    screenshot, width, height = _run(sess.capture_screen())

    assert screenshot == FAKE_PNG
    assert (width, height) == (1080, 2340)
    assert sess.resolve_ref("@e1", state_id=observed.state_id) == (970, 150)
    assert _run(sess.observe(mode="semantic")).state_id == "s2"


def test_observe_unknown_mode_raises():
    sess = _session()
    with pytest.raises(DeviceError):
        _run(sess.observe(mode="bogus"))


def test_state_id_increments_each_observe():
    sess = _session()
    o1 = _run(sess.observe())
    o2 = _run(sess.observe())
    assert (o1.state_id, o2.state_id) == ("s1", "s2")


def test_resolve_ref_returns_center():
    sess = _session()
    _run(sess.observe())
    # FAKE_XML: @e1 Button (Search) center ~ (970, 150), @e2 EditText.
    x, y = sess.resolve_ref("@e1")
    assert (x, y) == (970, 150)


def test_resolve_ref_accepts_variants():
    sess = _session()
    _run(sess.observe())
    assert sess.resolve_ref("e1") == sess.resolve_ref("@e1") == sess.resolve_ref("1")


def test_resolve_ref_without_snapshot_raises():
    sess = _session()
    with pytest.raises(DeviceError):
        sess.resolve_ref("@e1")


def test_resolve_unknown_ref_raises():
    sess = _session()
    _run(sess.observe())
    with pytest.raises(DeviceError):
        sess.resolve_ref("@e99")


def test_resolve_stale_state_id_raises():
    sess = _session()
    o1 = _run(sess.observe())
    _run(sess.observe())  # advance to s2
    with pytest.raises(DeviceError) as ei:
        sess.resolve_ref("@e1", state_id=o1.state_id)
    assert "stale" in str(ei.value)


def test_empty_a11y_surface_flags_empty_but_keeps_screenshot():
    sess = DeviceSession(session_key="DeviceUse", backend=FakeDeviceBackend(xml=""))
    obs = _run(sess.observe(mode="fused"))
    assert obs.empty is True
    assert obs.screenshot == FAKE_PNG  # visual floor still available
    # Screen size falls back to the backend probe when the outline is empty.
    assert obs.width == 1080


def test_lifecycle_start_shutdown_kill():
    backend = FakeDeviceBackend()
    sess = DeviceSession(session_key="DeviceUse", backend=backend)
    _run(sess.start())
    assert backend.started is True
    assert sess.closed is False
    _run(sess.shutdown())
    assert backend.shut is True
    assert sess.closed is True
    # kill is idempotent + sync.
    sess.kill()
    assert sess.closed is True


def test_lock_serializes_concurrent_observes():
    # Two concurrent observes must not interleave; both complete with distinct ids.
    sess = _session()

    async def go():
        return await asyncio.gather(sess.observe(), sess.observe())

    a, b = _run(go())
    assert {a.state_id, b.state_id} == {"s1", "s2"}
