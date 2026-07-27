#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pure-function tests for the browser's opt-in stealth (anti-bot-detection).

These do not launch Playwright/Chromium: they exercise the pure kwargs-builders
and the init-script constant, so they run everywhere. They lock in that stealth
is strictly opt-in (off by default) and that, when enabled, the launch/context
kwargs carry the tier-1 + tier-2 measures (real UA, locale, Accept-Language,
the AutomationControlled launch flag, and the ``navigator.webdriver`` patch).
"""
from __future__ import annotations

from mote.runtime.tools.dependency._browser import (
    _LOCALE_PROFILES,
    _TMALL_ACCEPT_LANGUAGE,
    _TMALL_REFERER,
    BrowserSession,
    _parse_proxy,
    _resolve_locale,
    _stealth_init_js,
    _tmall_compatible_headers,
)


def _session(stealth: bool, browser_locale: str = "en", proxy: str = "") -> BrowserSession:
    return BrowserSession(session_key="t", headless=True, stealth=stealth, browser_locale=browser_locale, proxy=proxy)


def test_stealth_off_by_default():
    assert _session(False).stealth is False
    # Default construction (no kwarg) is also non-stealth.
    assert BrowserSession(session_key="t").stealth is False


def test_launch_kwargs_plain_when_off():
    kwargs = _session(False)._launch_kwargs()
    assert kwargs == {"headless": True}
    assert "args" not in kwargs
    assert "ignore_default_args" not in kwargs


def test_launch_kwargs_add_anti_automation_flags_when_on():
    kwargs = _session(True)._launch_kwargs()
    assert kwargs["headless"] is True
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
    assert "--enable-automation" in kwargs["ignore_default_args"]


def test_context_kwargs_empty_when_off():
    assert _session(False)._context_kwargs(None) == {}


def test_context_kwargs_only_storage_state_when_off():
    state = {"cookies": [], "origins": []}
    assert _session(False)._context_kwargs(state) == {"storage_state": state}


def test_context_kwargs_fingerprint_overrides_when_on():
    kwargs = _session(True)._context_kwargs(None)
    # Real desktop UA, not the headless default.
    assert "HeadlessChrome" not in kwargs["user_agent"]
    assert "Chrome/" in kwargs["user_agent"]
    assert kwargs["locale"] == "en-US"
    assert kwargs["extra_http_headers"]["Accept-Language"].startswith("en-US")
    assert kwargs["timezone_id"]
    assert kwargs["viewport"]["width"] > 0


def test_context_kwargs_preserve_storage_state_when_on():
    state = {"cookies": [{"name": "s"}], "origins": []}
    kwargs = _session(True)._context_kwargs(state)
    assert kwargs["storage_state"] == state
    assert "user_agent" in kwargs  # stealth overrides applied alongside


def test_init_js_is_self_executing_and_patches_webdriver():
    # add_init_script runs the source as-is, so it must be a self-invoking IIFE.
    js = _stealth_init_js("en")
    assert js.strip().startswith("(()")
    assert js.strip().endswith(")();")
    assert "webdriver" in js
    assert "window.chrome" in js


def test_init_js_languages_match_locale_bundle():
    # navigator.languages must agree with the context locale (a mismatch is a
    # fingerprint tell): the en bundle injects en-US, the zh bundle zh-CN.
    en_js = _stealth_init_js("en")
    zh_js = _stealth_init_js("zh")
    assert '"en-US"' in en_js and '"zh-CN"' not in en_js
    assert '"zh-CN"' in zh_js and "zh" in zh_js


def test_resolve_locale_passthrough_and_default():
    assert _resolve_locale("en") == "en"
    assert _resolve_locale("zh") == "zh"


def test_resolve_locale_auto_from_env(monkeypatch):
    for var in ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    # No hint -> en fallback.
    assert _resolve_locale("auto") == "en"
    # A Chinese host env -> zh.
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert _resolve_locale("auto") == "zh"
    # Unknown value also runs through the auto path.
    assert _resolve_locale("fr") == "zh"


def test_context_kwargs_zh_bundle_is_consistent():
    kwargs = _session(True, browser_locale="zh")._context_kwargs(None)
    assert kwargs["locale"] == "zh-CN"
    assert kwargs["timezone_id"] == _LOCALE_PROFILES["zh"]["timezone_id"]
    assert kwargs["extra_http_headers"]["Accept-Language"].startswith("zh-CN")
    # UA + viewport are locale-independent (shared across bundles).
    assert "HeadlessChrome" not in kwargs["user_agent"]
    assert kwargs["viewport"]["width"] > 0


def test_tmall_headers_are_scoped_to_alibaba_retail_hosts():
    headers = _tmall_compatible_headers(
        "https://detail.tmall.com/item.htm?id=1",
        {"accept": "text/html"},
        is_navigation=True,
    )

    assert headers["accept"] == "text/html"
    assert headers["accept-language"] == _TMALL_ACCEPT_LANGUAGE
    assert headers["referer"] == _TMALL_REFERER
    assert _tmall_compatible_headers(
        "https://example.com/",
        {"accept": "text/html"},
        is_navigation=True,
    ) == {"accept": "text/html"}


def test_tmall_subresources_do_not_receive_a_navigation_referer():
    headers = _tmall_compatible_headers(
        "https://g.alicdn.com/resource.js",
        {"accept": "*/*"},
        is_navigation=False,
    )

    assert headers == {"accept": "*/*"}


# --- proxy / exit-IP --------------------------------------------------------


def test_parse_proxy_empty_is_none():
    assert _parse_proxy("") is None
    assert _parse_proxy("   ") is None


def test_parse_proxy_plain_host_port_defaults_to_http():
    # A bare host:port (no scheme) is treated as http://.
    assert _parse_proxy("127.0.0.1:8080") == {"server": "http://127.0.0.1:8080"}


def test_parse_proxy_keeps_scheme_and_splits_credentials():
    proxy = _parse_proxy("http://user:pass@proxy.example:3128")
    # Credentials must be split OUT of the server URL (Playwright's contract).
    assert proxy == {
        "server": "http://proxy.example:3128",
        "username": "user",
        "password": "pass",
    }


def test_parse_proxy_supports_socks5():
    assert _parse_proxy("socks5://10.0.0.1:1080") == {"server": "socks5://10.0.0.1:1080"}


def test_parse_proxy_unparseable_is_none():
    # No host -> disabled rather than a crashing launch.
    assert _parse_proxy("http://") is None


def test_launch_kwargs_omit_proxy_when_unset():
    assert "proxy" not in _session(False)._launch_kwargs()
    assert "proxy" not in _session(True)._launch_kwargs()


def test_launch_kwargs_include_proxy_independent_of_stealth():
    # Proxy attaches at launch regardless of stealth.
    plain = _session(False, proxy="http://user:pass@proxy.example:3128")._launch_kwargs()
    assert plain["proxy"] == {
        "server": "http://proxy.example:3128",
        "username": "user",
        "password": "pass",
    }
    stealthy = _session(True, proxy="socks5://10.0.0.1:1080")._launch_kwargs()
    assert stealthy["proxy"] == {"server": "socks5://10.0.0.1:1080"}
    # Stealth flags still present alongside the proxy.
    assert "--disable-blink-features=AutomationControlled" in stealthy["args"]
