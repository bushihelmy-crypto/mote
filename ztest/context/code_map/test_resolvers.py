"""Per-language module⇄file resolvers — the arithmetic that draws cross-file edges.

Each resolver answers the same four questions (``import_roots`` /
``module_to_path`` / ``module_candidates`` / ``is_relative``) in its language's
own path law. These tests build a tiny on-disk tree per language and assert the
central round-trip: a real import resolves to the real target file, and an
external / unresolvable import resolves to ``None`` (dropped, never guessed).
"""

from __future__ import annotations

import os

from mote.runtime.code_map.providers.resolvers.cfamily import CIncludeResolver
from mote.runtime.code_map.providers.resolvers.go import GoModuleResolver
from mote.runtime.code_map.providers.resolvers.java import JavaModuleResolver
from mote.runtime.code_map.providers.resolvers.javascript import JsModuleResolver
from mote.runtime.code_map.providers.resolvers.rust import RustModuleResolver


def _write(path: str, content: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_js_resolver_stem_and_index(tmp_path):
    r = JsModuleResolver()
    util = _write(str(tmp_path / "src" / "util.ts"))
    _write(str(tmp_path / "src" / "pkg" / "index.js"))
    roots: set[str] = set()
    # A relative spec is pre-resolved to an absolute stem by the extractor.
    assert r.module_to_path(str(tmp_path / "src" / "util"), roots) == util
    assert r.module_to_path(str(tmp_path / "src" / "pkg"), roots) == str(tmp_path / "src" / "pkg" / "index.js")
    assert r.module_to_path("lodash", roots) is None  # bare package → external
    assert r.module_candidates(util) == {str(tmp_path / "src" / "util")}
    assert r.is_relative("./x") is False


def test_go_resolver_import_path_to_package(tmp_path):
    r = GoModuleResolver()
    _write(str(tmp_path / "go.mod"), "module example.com/repo\n")
    pkg_file = _write(str(tmp_path / "util" / "u.go"), "package util\n")
    main = _write(str(tmp_path / "main.go"), "package main\n")
    roots = r.import_roots([main, pkg_file])
    assert r.module_to_path("example.com/repo/util", roots) == pkg_file
    assert r.module_to_path("fmt", roots) is None  # stdlib → external
    assert r.module_candidates(pkg_file) == {"example.com/repo/util"}


def test_java_resolver_package_anchored(tmp_path):
    r = JavaModuleResolver()
    root = tmp_path / "src" / "main" / "java"
    helper = _write(str(root / "com" / "foo" / "Helper.java"), "package com.foo;\n")
    app = _write(str(root / "com" / "foo" / "App.java"), "package com.foo;\n")
    roots = r.import_roots([app, helper])
    assert r.module_to_path("com.foo.Helper", roots) == helper
    assert r.module_to_path("java.util.List", roots) is None  # JDK → external
    assert r.module_candidates(helper) == {"com.foo.Helper"}
    assert r.is_relative("com.foo.Helper") is False


def test_rust_resolver_crate_path(tmp_path):
    r = RustModuleResolver()
    _write(str(tmp_path / "Cargo.toml"), "[package]\nname='x'\n")
    util = _write(str(tmp_path / "src" / "util.rs"))
    main = _write(str(tmp_path / "src" / "main.rs"))
    roots = r.import_roots([main, util])
    assert r.module_to_path("crate::util", roots) == util
    assert r.module_to_path("std::fmt", roots) is None  # external crate
    # A ``mod name;`` is pre-resolved to an absolute stem the resolver probes.
    assert r.module_to_path(str(tmp_path / "src" / "util"), roots) == util


def test_c_include_resolver_quoted_only(tmp_path):
    r = CIncludeResolver()
    header = _write(str(tmp_path / "src" / "util.h"), "int f(void);\n")
    roots: set[str] = set()
    # The extractor resolves ``#include "util.h"`` to this absolute path.
    assert r.module_to_path(header, roots) == header
    # A system ``<stdio.h>`` include was dropped (non-absolute) → external.
    assert r.module_to_path("stdio.h", roots) is None
    assert r.module_to_path(str(tmp_path / "src" / "missing.h"), roots) is None
    assert r.module_candidates(header) == {header}
    assert r.is_relative("stdio.h") is False
