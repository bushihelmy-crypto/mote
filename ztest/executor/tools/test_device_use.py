#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the DeviceUse tool — action dispatch over a FakeDeviceBackend.

No real device / adb: a :class:`FakeDeviceBackend` (fixed PNG + uiautomator XML)
is injected by monkeypatching ``select_device_backend`` so the whole observe/act
contract is exercised deterministically. Asserts: action dispatch, tap-by-ref
coordinate resolution, stale-ref error, screenshot→ToolMedia, effect == EXTERNAL,
and the three observe modes.
"""
from __future__ import annotations

import pytest

from mote.contracts.tool.effects import ToolEffect
from mote.product.toolsets.builtin import device_use as device_use_mod
from mote.product.toolsets.builtin.device_use import DeviceUse
from mote.runtime.interactive.device.backend import DeviceError
from mote.runtime.tools.tool_result import ToolError, ToolResult
from mote.ztest.executor.dependency.device_fakes import FAKE_PNG, FakeDeviceBackend, RaisingDeviceBackend
from mote.ztest.executor.tools.conftest import CapRole, bind, run


@pytest.fixture
def fake_backend(monkeypatch):
    """Route ``select_device_backend`` to a fresh FakeDeviceBackend and expose it."""
    backend = FakeDeviceBackend()
    monkeypatch.setattr(device_use_mod, "select_device_backend", lambda _cfg: backend)
    return backend


@pytest.fixture
def tool(fake_backend):
    """A DeviceUse bound to a CapRole + the injected fake backend."""
    role = CapRole()
    t = bind(DeviceUse(), role=role)
    # White-box test handle: bypass the production stateful-field guard
    # deliberately; tool code never receives or writes this attribute.
    object.__setattr__(t, "_role", role)
    return t


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def test_effect_is_external():
    assert DeviceUse.resolve_effect() is ToolEffect.EXTERNAL
    assert DeviceUse.effect is ToolEffect.EXTERNAL


def test_stateful_and_high_risk():
    assert DeviceUse.stateful is True
    assert DeviceUse.risk_level == "high"


def test_requires_capabilities():
    assert set(DeviceUse.requires) == {
        "get_artifact_publisher",
        "get_runtime_host",
        "get_device_config",
        "get_default_model",
        "handoff_runtime",
    }


# ---------------------------------------------------------------------------
# observe — three modes
# ---------------------------------------------------------------------------


def test_observe_fused_returns_outline_and_screenshot(tool):
    result = run(tool.call(action="observe"))
    assert isinstance(result, ToolResult)
    # screenshot rides as ToolMedia (image)
    assert len(result.media) == 1
    assert result.media[0].kind == "image"
    assert result.media[0].mime == "image/png"
    assert result.media[0].artifact is not None
    assert run(tool._role.artifact_store.read(result.media[0].artifact)) == FAKE_PNG
    # outline text present (Search button + edit field carry refs)
    assert "@e1" in result.output
    assert "@e2" in result.output
    assert result.data["state_id"] == "s1"


def test_observe_semantic_has_no_screenshot(tool):
    result = run(tool.call(action="observe", mode="semantic"))
    assert result.media == []
    assert "@e1" in result.output


def test_observe_visual_has_screenshot_no_outline(tool):
    result = run(tool.call(action="observe", mode="visual"))
    assert len(result.media) == 1
    # visual mode skips the a11y outline entirely
    assert "@e1" not in result.output
    assert result.data["empty"] is True


def test_observe_visual_refused_without_vision_model(fake_backend, monkeypatch):
    role = CapRole(default_model="text-only-model")
    monkeypatch.setattr("mote.product.toolsets.builtin.device_use.supports_vision", lambda _m: False)
    t = bind(DeviceUse(), role=role)
    with pytest.raises(Exception) as ei:  # ToolNotConfiguredError
        run(t.call(action="observe", mode="visual"))
    assert "screenshot" in str(ei.value).lower()


def test_observe_fused_drops_screenshot_without_vision(fake_backend, monkeypatch):
    role = CapRole(default_model="text-only-model")
    monkeypatch.setattr("mote.product.toolsets.builtin.device_use.supports_vision", lambda _m: False)
    t = bind(DeviceUse(), role=role)
    result = run(t.call(action="observe", mode="fused"))
    # fused degrades gracefully: no image, but the outline still returns
    assert result.media == []
    assert "@e1" in result.output


def test_observe_empty_outline_flags_and_keeps_screenshot(monkeypatch):
    backend = FakeDeviceBackend(xml="")
    monkeypatch.setattr(device_use_mod, "select_device_backend", lambda _cfg: backend)
    t = bind(DeviceUse(), role=CapRole())
    result = run(t.call(action="observe", mode="fused"))
    assert result.data["empty"] is True
    assert len(result.media) == 1
    assert "no accessibility outline" in result.output


# ---------------------------------------------------------------------------
# tap / long_press — by ref and by coordinate
# ---------------------------------------------------------------------------


def test_tap_by_ref_resolves_center(tool, fake_backend):
    run(tool.call(action="observe"))
    run(tool.call(action="tap", ref="@e1"))
    # @e1 = Search button [900,120][1040,180] -> center (970, 150)
    assert ("tap", 970, 150) in fake_backend.calls


def test_tap_by_coordinate(tool, fake_backend):
    run(tool.call(action="tap", x=300, y=400))
    assert ("tap", 300, 400) in fake_backend.calls


def test_tap_without_ref_or_coordinate_errors(tool):
    with pytest.raises(ToolError):
        run(tool.call(action="tap"))


def test_long_press_by_ref(tool, fake_backend):
    run(tool.call(action="observe"))
    run(tool.call(action="long_press", ref="@e2"))
    # @e2 = EditText [40,200][1040,280] -> center (540, 240)
    assert any(c[0] == "long_press" and c[1:3] == (540, 240) for c in fake_backend.calls)


def test_stale_ref_errors(tool):
    run(tool.call(action="observe"))  # state s1
    run(tool.call(action="observe"))  # state s2 — refs from s1 now stale
    with pytest.raises(ToolError) as ei:
        run(tool.call(action="tap", ref="@e1", state_id="s1"))
    assert "stale" in str(ei.value).lower() or "observe again" in str(ei.value).lower()


def test_unknown_ref_errors(tool):
    run(tool.call(action="observe"))
    with pytest.raises(ToolError):
        run(tool.call(action="tap", ref="@e99"))


# ---------------------------------------------------------------------------
# swipe / scroll
# ---------------------------------------------------------------------------


def test_swipe(tool, fake_backend):
    run(tool.call(action="swipe", x=100, y=200, x2=100, y2=800))
    assert any(c[0] == "swipe" and c[1:5] == (100, 200, 100, 800) for c in fake_backend.calls)


def test_swipe_missing_endpoints_errors(tool):
    with pytest.raises(ToolError):
        run(tool.call(action="swipe", x=100, y=200))


def test_scroll_down_issues_swipe(tool, fake_backend):
    run(tool.call(action="scroll", direction="down"))
    assert any(c[0] == "swipe" for c in fake_backend.calls)


def test_scroll_bad_direction_errors(tool):
    with pytest.raises(ToolError):
        run(tool.call(action="scroll", direction="sideways"))


# ---------------------------------------------------------------------------
# type / key / open_app / list_apps
# ---------------------------------------------------------------------------


def test_type_text(tool, fake_backend):
    run(tool.call(action="type", text="中文测试"))
    assert ("input_text", "中文测试") in fake_backend.calls


def test_type_requires_text(tool):
    with pytest.raises(ToolError):
        run(tool.call(action="type"))


def test_key(tool, fake_backend):
    run(tool.call(action="key", key="BACK"))
    assert ("key", "BACK") in fake_backend.calls


def test_open_app(tool, fake_backend):
    run(tool.call(action="open_app", app="com.android.settings"))
    assert ("open_app", "com.android.settings") in fake_backend.calls


def test_list_apps(tool, fake_backend):
    out = run(tool.call(action="list_apps"))
    assert "com.android.settings" in out


# ---------------------------------------------------------------------------
# wait / close
# ---------------------------------------------------------------------------


def test_wait_returns_without_session(monkeypatch):
    # wait must not require a device — no backend selection happens.
    def _boom(_cfg):
        raise AssertionError("wait should not select a backend")

    monkeypatch.setattr(device_use_mod, "select_device_backend", _boom)
    t = bind(DeviceUse(), role=CapRole())
    out = run(t.call(action="wait", seconds=0.0))
    assert "waited" in out


def test_close_shuts_down_session(tool, fake_backend):
    run(tool.call(action="observe"))
    out = run(tool.call(action="close"))
    assert "closed" in out
    assert fake_backend.shut is True


def test_close_without_session(tool):
    out = run(tool.call(action="close"))
    assert "no device" in out


# ---------------------------------------------------------------------------
# session reuse + error surfacing
# ---------------------------------------------------------------------------


def test_session_reused_across_calls(tool, fake_backend):
    run(tool.call(action="observe"))
    run(tool.call(action="observe"))
    # started exactly once — the session persisted in RuntimeHost between calls
    assert fake_backend.started is True
    assert tool.get_runtime_host().descriptor("device:default").revision == 2


def test_unknown_action_errors(tool):
    with pytest.raises(ToolError) as ei:
        run(tool.call(action="fly"))
    assert "unknown device action" in str(ei.value).lower()


def test_device_error_surfaces_as_tool_error(monkeypatch):
    backend = RaisingDeviceBackend()
    monkeypatch.setattr(device_use_mod, "select_device_backend", lambda _cfg: backend)
    t = bind(DeviceUse(), role=CapRole())
    with pytest.raises(ToolError):
        run(t.call(action="tap", x=1, y=1))
