#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Behavioral tests for :mod:`mote.runtime.file_paths`."""
from __future__ import annotations

import os

from mote.runtime.file_paths import display_path, path_to_uri, uri_to_path


class TestUriRoundTrip:
    def test_path_to_uri_is_file_scheme(self):
        uri = path_to_uri("/tmp/some file.py")
        assert uri.startswith("file://")

    def test_round_trip_preserves_absolute_path(self):
        abspath = os.path.abspath("/tmp/some file.py")
        assert uri_to_path(path_to_uri(abspath)) == abspath

    def test_uri_to_path_unquotes(self):
        assert uri_to_path("file:///tmp/a%20b.py") == "/tmp/a b.py"

    def test_non_file_uri_passes_through(self):
        assert uri_to_path("/plain/path.py") == "/plain/path.py"


class TestDisplayPath:
    def test_relativizes_against_cwd(self):
        assert display_path("/repo/pkg/mod.py", "/repo") == os.path.join("pkg", "mod.py")

    def test_no_cwd_passes_through(self):
        assert display_path("/repo/pkg/mod.py", None) == "/repo/pkg/mod.py"

    def test_empty_cwd_passes_through(self):
        assert display_path("/repo/pkg/mod.py", "") == "/repo/pkg/mod.py"
