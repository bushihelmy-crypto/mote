#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.compress.pytest.PytestCompressor``."""
from __future__ import annotations

from metagpt.executor.compress.pytest import PytestCompressor

# A representative failing run with lots of progress noise.
OUTPUT = (
    "============================= test session starts =============================\n"
    "platform linux -- Python 3.11.0, pytest-8.0.0\n"
    "collected 100 items\n"
    "\n"
    + "".join(f"tests/test_mod{i}.py .......... [ {i}%]\n" for i in range(1, 90))
    + "tests/test_bad.py ....F..... [100%]\n"
    "\n"
    "=================================== FAILURES ===================================\n"
    "_________________________________ test_thing __________________________________\n"
    "\n"
    "    def test_thing():\n"
    ">       assert compute() == 42\n"
    "E       assert 0 == 42\n"
    "\n"
    "tests/test_bad.py:15: AssertionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_bad.py::test_thing - assert 0 == 42\n"
    "========================= 1 failed, 99 passed in 3.21s =========================\n"
)


class TestPytestCompressor:
    def test_applies_and_shrinks(self):
        r = PytestCompressor().compress(OUTPUT, argv=["pytest"])
        assert r.applied is True
        assert r.compressed_chars < r.original_chars
        assert r.label == "pytest"

    def test_failure_block_preserved_verbatim(self):
        r = PytestCompressor().compress(OUTPUT, argv=["pytest"])
        assert "=== FAILURES ===" in r.text or "FAILURES" in r.text
        assert "def test_thing():" in r.text
        assert "assert compute() == 42" in r.text
        assert "E       assert 0 == 42" in r.text
        assert "tests/test_bad.py:15: AssertionError" in r.text

    def test_summary_lines_preserved(self):
        r = PytestCompressor().compress(OUTPUT, argv=["pytest"])
        assert "FAILED tests/test_bad.py::test_thing - assert 0 == 42" in r.text
        assert "1 failed, 99 passed in 3.21s" in r.text

    def test_progress_dropped(self):
        r = PytestCompressor().compress(OUTPUT, argv=["pytest"])
        # None of the ``[ NN%]`` progress lines survive.
        assert "[ 50%]" not in r.text
        assert "tests/test_mod50.py" not in r.text

    def test_collected_count_kept(self):
        r = PytestCompressor().compress(OUTPUT, argv=["pytest"])
        assert "collected 100 items" in r.text

    def test_all_passing_keeps_result_line(self):
        passing = (
            "============================= test session starts =============================\n"
            "collected 3 items\n"
            "\n"
            "tests/test_ok.py ... [100%]\n"
            "\n"
            "============================== 3 passed in 0.05s ==============================\n"
        )
        r = PytestCompressor().compress(passing, argv=["pytest"])
        assert "3 passed in 0.05s" in r.text
        assert "[100%]" not in r.text
