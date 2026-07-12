#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Catalog completeness + placeholder-consistency guards.

These are the tests that make a *missing* or *drifted* translation a hard
failure instead of a silent ``⟦key⟧`` marker or a ``KeyError`` at render time:

* every shipped catalog covers exactly ``keys.ALL_KEYS`` (no gaps, no strays);
* every locale uses the same set of ``{placeholders}`` for a given id, so a
  call site's kwargs render in any language.
"""
from __future__ import annotations

import re
from typing import Dict, Set

import pytest

from mote.common.i18n import keys as K
from mote.common.i18n.catalog import en, zh

_CATALOGS: Dict[str, Dict[str, str]] = {"zh": zh.CATALOG, "en": en.CATALOG}

# A top-level ``{name}`` or the argument name of a ``{name, plural/select, …}``:
# the word right after ``{`` up to the first ``,`` or ``}``. Case-body selectors
# like ``one{# line}`` never match — a ``{`` is always followed by a selector
# word then ``{``, and we only capture the token before ``,``/``}``.
_ARG_RE = re.compile(r"\{\s*([A-Za-z_]\w*)\s*[,}]")


def _placeholders(pattern: str) -> Set[str]:
    return set(_ARG_RE.findall(pattern))


@pytest.mark.parametrize("code", sorted(_CATALOGS))
def test_catalog_covers_all_keys_exactly(code: str) -> None:
    catalog = _CATALOGS[code]
    expected = set(K.ALL_KEYS)
    actual = set(catalog)
    assert actual == expected, (
        f"{code} catalog drift: " f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
    )


def test_all_keys_has_no_duplicates() -> None:
    assert len(K.ALL_KEYS) == len(set(K.ALL_KEYS))


@pytest.mark.parametrize("key", K.ALL_KEYS)
def test_placeholders_consistent_across_locales(key: str) -> None:
    per_locale = {code: _placeholders(cat[key]) for code, cat in _CATALOGS.items()}
    reference = per_locale["en"]
    for code, names in per_locale.items():
        assert names == reference, f"{key}: {code} placeholders {names} != en {reference}"


def test_verb_keys_are_all_catalogued() -> None:
    for key in K.STATUS_VERB_KEYS:
        assert key in en.CATALOG and key in zh.CATALOG
