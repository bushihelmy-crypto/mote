#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.runtime.content_hashing`."""
from __future__ import annotations

import hashlib

from mote.runtime.content_hashing import content_hash


class TestContentHash:
    def test_matches_sha256_of_utf8(self):
        text = "def foo():\n    return 1\n"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert content_hash(text) == expected

    def test_empty_string(self):
        assert content_hash("") == hashlib.sha256(b"").hexdigest()

    def test_deterministic(self):
        text = "some source\n"
        assert content_hash(text) == content_hash(text)

    def test_unicode(self):
        text = "# 注释\nx = '值'\n"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert content_hash(text) == expected

    def test_distinct_inputs_differ(self):
        assert content_hash("a") != content_hash("b")

    def test_is_hex_digest(self):
        digest = content_hash("anything")
        assert len(digest) == 64
        int(digest, 16)  # raises if not hex
