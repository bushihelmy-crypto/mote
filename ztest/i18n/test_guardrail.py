#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Zero-debt guardrail: no hard-coded CJK in Product presentation adapters.

The human display layer (``product/presentation`` + ``product/interfaces``) must route every
human string through :func:`mote.product.i18n.t`, so a locale switch reaches all
of it. This test walks the AST of every module there and fails on a CJK
character inside a *real* string literal — a value that gets assigned, passed,
or f-string-rendered. Docstrings and bare string-expression statements (used as
prose/comments) are exempt, since they are not user-facing output.

This is the long-lived enforcement that keeps the "0 negative-space debt"
property true: a future hard-coded ``"读取 N 行"`` fails CI instead of silently
shipping an un-switchable string.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from mote.product.paths import MOTE_PACKAGE_DIR

# CJK Unified Ideographs + Extension-A + fullwidth/half-width forms: enough to
# catch any hard-coded Chinese sentence or its fullwidth punctuation (（），。).
_CJK = (
    tuple(
        range(0x3400, 0x4DBF + 1),
    )
    + tuple(range(0x4E00, 0x9FFF + 1))
    + tuple(range(0xFF00, 0xFFEF + 1))
)
_CJK_SET = frozenset(chr(c) for c in _CJK)

_SCAN_DIRS = ("product/presentation", "product/interfaces")


def _has_cjk(text: str) -> bool:
    return any(ch in _CJK_SET for ch in text)


def _display_modules() -> List[Path]:
    root = MOTE_PACKAGE_DIR
    # PACKAGE_DIR is the package dir itself; presentation is Product-owned.
    # Fall back to walking up from this test file if the layout differs so the
    # guardrail is robust to checkout naming.
    if not (root / "product/presentation").exists():
        root = Path(__file__).resolve().parents[2]
    files: List[Path] = []
    for rel in _SCAN_DIRS:
        files.extend((root / rel).rglob("*.py"))
    return files


def _string_literals(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    """Yield ``(lineno, value)`` for every non-exempt string literal in *tree*.

    Exempt: docstrings and bare string-expression statements (``ast.Expr`` whose
    value is a str/JoinedStr) — prose, not output. Everything else (assignments,
    call args, f-string literal parts) is a candidate.
    """
    exempt: set = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            # Only statement *lists* hold bare-string prose; ``body`` on Lambda/
            # IfExp is a single expression node, not a block — skip those.
            if not isinstance(block, list):
                continue
            for stmt in block:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Constant, ast.JoinedStr)):
                    exempt.add(id(stmt.value))
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr) and id(node) not in exempt:
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield getattr(part, "lineno", node.lineno), part.value


@pytest.mark.parametrize("path", _display_modules(), ids=lambda p: str(p.name))
def test_no_hardcoded_cjk_in_display_layer(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [(ln, val) for ln, val in _string_literals(tree) if _has_cjk(val)]
    assert not offenders, f"{path}: hard-coded CJK string literal(s) — route through i18n.t():\n" + "\n".join(
        f"  line {ln}: {val!r}" for ln, val in offenders
    )


def test_guardrail_actually_scans_files() -> None:
    # Guard the guard: a broken path glob (0 files) would vacuously pass.
    assert len(_display_modules()) >= 5
