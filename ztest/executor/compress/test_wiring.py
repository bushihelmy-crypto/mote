#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Wiring tests for ``compress.tool_output`` — ``compress_tool_result`` /
``_command_for_compression``.

Exercises the tool-side glue: shell-tool routing, the marker line + raw
persistence, and the skip paths (media, already-persisted, config-off, non
shell tools). ``spill.persist_result`` is redirected to ``tmp_path``.
"""
from __future__ import annotations

from mote.contracts.config.tool import PERSISTED_OUTPUT_OPEN_TAG, ToolResultLimitConfig
from mote.runtime.resources import spill
from mote.runtime.tools.compress.tool_output import _command_for_compression, compress_tool_result
from mote.runtime.tools.tool_result import ToolResult
from mote.ztest.artifact_fakes import artifact_media

# A compressible pytest blob (routes + shrinks), well above the min floor.
_PYTEST = (
    "============================= test session starts =============================\n"
    "collected 200 items\n"
    + "".join(f"tests/test_module_number_{i}.py .......... [ {i}%]\n" for i in range(200))
    + "=================================== FAILURES ===================================\n"
    "    def test_x():\n"
    ">       assert 1 == 2\n"
    "E       assert 1 == 2\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/t0.py::test_x - assert 1 == 2\n"
    "========================= 1 failed, 39 passed in 1.0s =========================\n"
)


def _compress(
    result: ToolResult,
    name: str,
    args: dict,
    config: ToolResultLimitConfig | None = None,
) -> ToolResult:
    """Invoke the module-level entry with a default config + session id."""
    return compress_tool_result(result, name, args, session_id="sess", config=config or ToolResultLimitConfig())


def _persist_to_tmp(monkeypatch, tmp_path):
    """Point ``persist_result`` at ``tmp_path`` and record what it wrote."""
    written = {}

    def fake_persist(output, result_id, session_id, base_dir):
        path = tmp_path / f"{result_id}.txt"
        path.write_text(output, encoding="utf-8")
        written["path"] = str(path)
        written["output"] = output
        written["result_id"] = result_id
        return str(path)

    monkeypatch.setattr(spill, "persist_result", fake_persist)
    return written


class TestBashRouting:
    def test_bash_compressed_with_marker_and_raw_persist(self, monkeypatch, tmp_path):
        written = _persist_to_tmp(monkeypatch, tmp_path)
        result = ToolResult(output=_PYTEST, success=True)

        out = _compress(result, "Bash", {"command": "pytest tests/"})

        # Marker prepended, naming the raw file.
        assert out.output.startswith("[compressed: pytest;")
        assert "full output:" in out.output.splitlines()[0]
        # Failure signal survives inside the compressed body.
        assert "FAILED tests/t0.py::test_x" in out.output
        # Full original persisted under the ``raw-`` namespace.
        assert written["result_id"].startswith("raw-")
        assert written["output"] == _PYTEST
        # ``success`` untouched.
        assert out.success is True

    def test_bash_success_never_changed_on_failure(self, monkeypatch, tmp_path):
        _persist_to_tmp(monkeypatch, tmp_path)
        result = ToolResult(output=_PYTEST, success=False)
        out = _compress(result, "Bash", {"command": "pytest"})
        assert out.success is False


class TestTerminalRouting:
    def test_terminal_uses_first_input_line(self, monkeypatch, tmp_path):
        _persist_to_tmp(monkeypatch, tmp_path)
        result = ToolResult(output=_PYTEST, success=True)
        out = _compress(result, "Terminal", {"input": "pytest tests/\n"})
        assert out.output.startswith("[compressed: pytest;")

    def test_terminal_empty_input_skipped(self):
        result = ToolResult(output=_PYTEST, success=True)
        out = _compress(result, "Terminal", {"input": "   "})
        assert out.output == _PYTEST  # not compressed


class TestJupyterRouting:
    def test_shell_magic_compressed(self, monkeypatch, tmp_path):
        _persist_to_tmp(monkeypatch, tmp_path)
        result = ToolResult(output=_PYTEST, success=True)
        out = _compress(result, "Jupyter", {"code": "!pytest tests/\n"})
        assert out.output.startswith("[compressed: pytest;")

    def test_plain_python_not_compressed(self):
        result = ToolResult(output=_PYTEST, success=True)
        # A plain Python first line must never be sniffed as a command.
        out = _compress(result, "Jupyter", {"code": "print('pytest tests/')\n"})
        assert out.output == _PYTEST


class TestSkipPaths:
    def test_media_result_skipped(self):
        result = ToolResult(
            output=_PYTEST,
            success=True,
            media=[artifact_media("image", "base64data")],
        )
        out = _compress(result, "Bash", {"command": "pytest"})
        assert out.output == _PYTEST

    def test_already_persisted_skipped(self):
        body = f"{PERSISTED_OUTPUT_OPEN_TAG}\nsaved earlier\n</persisted-output>"
        result = ToolResult(output=body, success=True)
        out = _compress(result, "Bash", {"command": "pytest"})
        assert out.output == body

    def test_config_off_skipped(self):
        cfg = ToolResultLimitConfig(enable_output_compression=False)
        result = ToolResult(output=_PYTEST, success=True)
        out = _compress(result, "Bash", {"command": "pytest"}, config=cfg)
        assert out.output == _PYTEST

    def test_non_shell_tool_skipped(self):
        result = ToolResult(output=_PYTEST, success=True)
        out = _compress(result, "Read", {"file_path": "/x"})
        assert out.output == _PYTEST


class TestCommandForCompression:
    def test_bash_command(self):
        assert _command_for_compression("Bash", {"command": "git status"}) == "git status"

    def test_terminal_first_line(self):
        assert _command_for_compression("Terminal", {"input": "git log\nmore"}) == "git log"

    def test_other_tool_none(self):
        assert _command_for_compression("Grep", {"pattern": "x"}) is None

    def test_jupyter_shell_magic_strips_bang(self):
        assert _command_for_compression("Jupyter", {"code": "!git diff\nx=1"}) == "git diff"

    def test_jupyter_plain_python_none(self):
        assert _command_for_compression("Jupyter", {"code": "import os\nos.getcwd()"}) is None

    def test_jupyter_bare_bang_none(self):
        assert _command_for_compression("Jupyter", {"code": "!   "}) is None

    def test_python_alias_routed(self):
        assert _command_for_compression("Python", {"code": "!pytest"}) == "pytest"
