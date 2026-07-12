#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime seam: plural rules, negotiation/fallback, detect, ``t()`` + scoping."""
from __future__ import annotations

from mote.common.i18n import keys as K
from mote.common.i18n import t, use_locale
from mote.common.i18n.detect import resolve_locale_code
from mote.common.i18n.locale import negotiate
from mote.common.i18n.plurals import OTHER, plural_category
from mote.common.i18n.runtime import BASE_LOCALE, DEFAULT_LOCALE, _lookup, current_locale


def test_plural_categories() -> None:
    assert plural_category("en", 1) == "one"
    assert plural_category("en", 0) == "other"
    assert plural_category("en", 2) == "other"
    assert plural_category("zh", 1) == OTHER
    assert plural_category("zh", 99) == OTHER
    # Unknown language falls back to the English rule.
    assert plural_category("xx", 1) == "one"


def test_negotiate_exact_base_and_default() -> None:
    assert negotiate("en").code == "en"
    assert negotiate("zh").code == "zh"
    assert negotiate("zh-Hans").code == "zh"  # base-language match
    assert negotiate("en_US").code == "en"  # POSIX underscore
    assert negotiate("fr", default="zh").code == "zh"  # unshipped → default
    assert negotiate(None, default="en").code == "en"  # empty → default


def test_detect_precedence_config_over_env() -> None:
    env = {"LANG": "zh_CN.UTF-8"}
    assert resolve_locale_code("en", env) == "en"  # explicit config wins
    assert resolve_locale_code("auto", env) == "zh_CN"  # auto → env
    assert resolve_locale_code(None, env) == "zh_CN"
    assert resolve_locale_code(None, {"LANG": "C"}) is None  # neutral → None
    assert resolve_locale_code(None, {}) is None


def test_defaults() -> None:
    assert DEFAULT_LOCALE == "zh"
    assert BASE_LOCALE == "en"


def test_use_locale_scopes_and_restores() -> None:
    with use_locale("en"):
        assert current_locale().code == "en"
        with use_locale("zh"):
            assert current_locale().code == "zh"
        assert current_locale().code == "en"


def test_t_translates_under_active_locale() -> None:
    with use_locale("zh"):
        assert t(K.STATUS_IDLE) == "就绪"
    with use_locale("en"):
        assert t(K.STATUS_IDLE) == "ready"


def test_t_missing_key_returns_marker() -> None:
    assert t("no.such.key") == "\u27e6no.such.key\u27e7"


def test_lookup_falls_back_to_base_locale() -> None:
    # A key present only in en (BASE_LOCALE) still resolves for a zh lookup path
    # via the fallback chain (loc.code → loc.language → BASE_LOCALE).
    from mote.common.i18n.runtime import register_catalog

    register_catalog("en", {"test.only_en": "base-only"})
    zh_loc = negotiate("zh")
    assert _lookup(zh_loc, "test.only_en") == "base-only"
