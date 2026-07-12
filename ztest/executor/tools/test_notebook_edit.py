#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the NotebookEdit tool (``metagpt.executor.tools.notebook_edit``).

Covers replace / insert / delete, cell-id resolution (real id + positional
"cell-N"), the replace-past-end -> insert promotion, code-cell output clearing,
and the validation / read-before-edit guards. Pure helpers are tested directly.
"""
from __future__ import annotations

import json
import os

import pytest

from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools.notebook_edit import (
    NotebookEdit,
    _find_cell_index,
    _parse_cell_id,
    _supports_cell_ids,
)

from .conftest import CapRole, bind, run, write_file, mark_read


def _nb(*cells, nbformat_minor=5):
    return {"cells": list(cells), "nbformat": 4, "nbformat_minor": nbformat_minor}


def _code(source, cell_id=None, outputs=None):
    cell = {"cell_type": "code", "source": source, "execution_count": 1, "outputs": outputs or [{"output_type": "stream", "text": "old\n"}], "metadata": {}}
    if cell_id:
        cell["id"] = cell_id
    return cell


def _md(source, cell_id=None):
    cell = {"cell_type": "markdown", "source": source, "metadata": {}}
    if cell_id:
        cell["id"] = cell_id
    return cell


def _ready(workspace, notebook):
    p = write_file(workspace / "nb.ipynb", json.dumps(notebook))
    role = CapRole()
    mark_read(role, p)
    return bind(NotebookEdit(), role), p


def _load(p):
    return json.loads(open(p, encoding="utf-8").read())


def _edit(tool, **kwargs):
    return run(tool.call(**kwargs))


class TestReplace:
    def test_replace_by_real_id_clears_outputs(self, workspace):
        tool, p = _ready(workspace, _nb(_code("print(1)", cell_id="abc")))
        out = _edit(tool, notebook_path=p, new_source="print(2)", cell_id="abc")
        assert "Updated cell abc" in out
        cell = _load(p)["cells"][0]
        assert cell["source"] == "print(2)"
        assert cell["execution_count"] is None
        assert cell["outputs"] == []

    def test_replace_by_positional_id(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a"), _md("# b")))
        _edit(tool, notebook_path=p, new_source="# changed", cell_id="cell-1")
        assert _load(p)["cells"][1]["source"] == "# changed"

    def test_replace_retypes_cell(self, workspace):
        tool, p = _ready(workspace, _nb(_code("x", cell_id="c1")))
        _edit(tool, notebook_path=p, new_source="# now markdown", cell_id="c1", cell_type="markdown")
        assert _load(p)["cells"][0]["cell_type"] == "markdown"

    def test_replace_missing_cell_id_raises(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a", cell_id="x")))
        with pytest.raises(ToolError, match="not found in notebook"):
            _edit(tool, notebook_path=p, new_source="y", cell_id="does-not-exist")

    def test_replace_without_cell_id_raises(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a")))
        with pytest.raises(ToolError, match="cell_id must be specified"):
            _edit(tool, notebook_path=p, new_source="y")


class TestInsert:
    def test_insert_at_start_when_no_cell_id(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# existing")))
        out = _edit(tool, notebook_path=p, new_source="# new", cell_type="markdown", edit_mode="insert")
        cells = _load(p)["cells"]
        assert len(cells) == 2
        assert cells[0]["source"] == "# new"
        assert "Inserted a new markdown cell" in out

    def test_insert_after_named_cell(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a", cell_id="a"), _md("# b", cell_id="b")))
        _edit(tool, notebook_path=p, new_source="# mid", cell_type="markdown", cell_id="a", edit_mode="insert")
        cells = _load(p)["cells"]
        assert [c["source"] for c in cells] == ["# a", "# mid", "# b"]

    def test_insert_requires_cell_type(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a")))
        with pytest.raises(ToolError, match="cell_type is required"):
            _edit(tool, notebook_path=p, new_source="x", edit_mode="insert")

    def test_new_code_cell_is_well_formed(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a")))
        _edit(tool, notebook_path=p, new_source="print(1)", cell_type="code", edit_mode="insert")
        new = _load(p)["cells"][0]
        assert new["cell_type"] == "code"
        assert new["execution_count"] is None
        assert new["outputs"] == []


class TestDelete:
    def test_delete_removes_cell(self, workspace):
        tool, p = _ready(workspace, _nb(_md("# a", cell_id="a"), _md("# b", cell_id="b")))
        out = _edit(tool, notebook_path=p, new_source="", cell_id="a", edit_mode="delete")
        cells = _load(p)["cells"]
        assert len(cells) == 1
        assert cells[0]["source"] == "# b"
        assert "Deleted cell a" in out


class TestPromotion:
    def test_replace_one_past_end_promotes_to_insert(self, workspace):
        # cell-1 is one past the single cell at index 0 => promoted to insert.
        tool, p = _ready(workspace, _nb(_md("# a")))
        out = _edit(tool, notebook_path=p, new_source="# appended", cell_id="cell-1")
        cells = _load(p)["cells"]
        assert len(cells) == 2
        assert cells[1]["source"] == "# appended"
        assert "Inserted" in out


class TestNotebookGuards:
    def test_non_ipynb_refused(self, workspace):
        p = write_file(workspace / "a.txt", "{}")
        with pytest.raises(ToolError, match="not a Jupyter notebook"):
            _edit(NotebookEdit(), notebook_path=p, new_source="x", cell_id="c")

    def test_missing_file_raises(self, workspace):
        with pytest.raises(ToolError, match="notebook does not exist"):
            _edit(NotebookEdit(), notebook_path=str(workspace / "nope.ipynb"), new_source="x", cell_id="c")

    def test_invalid_edit_mode_raises(self, workspace):
        p = write_file(workspace / "nb.ipynb", json.dumps(_nb(_md("# a"))))
        with pytest.raises(ToolError, match="edit_mode must be"):
            _edit(NotebookEdit(), notebook_path=p, new_source="x", cell_id="c", edit_mode="frobnicate")

    def test_invalid_cell_type_raises(self, workspace):
        p = write_file(workspace / "nb.ipynb", json.dumps(_nb(_md("# a"))))
        with pytest.raises(ToolError, match="cell_type must be"):
            _edit(NotebookEdit(), notebook_path=p, new_source="x", cell_id="cell-0", cell_type="raw")

    def test_unread_notebook_blocked(self, workspace):
        p = write_file(workspace / "nb.ipynb", json.dumps(_nb(_md("# a", cell_id="a"))))
        tool = bind(NotebookEdit(), CapRole())  # not read
        with pytest.raises(ToolError, match="has not been read this session"):
            _edit(tool, notebook_path=p, new_source="y", cell_id="a")

    def test_invalid_json_raises(self, workspace):
        p = write_file(workspace / "nb.ipynb", "{bad json")
        with pytest.raises(ToolError, match="not a valid notebook"):
            _edit(NotebookEdit(), notebook_path=p, new_source="x", cell_id="c")


# --- Pure-helper unit tests --------------------------------------------------


class TestHelpers:
    def test_parse_cell_id_positional(self):
        assert _parse_cell_id("cell-7") == 7

    def test_parse_cell_id_non_positional(self):
        assert _parse_cell_id("abc123") is None

    def test_find_cell_index_by_real_id(self):
        cells = [{"id": "x"}, {"id": "y"}]
        assert _find_cell_index(cells, "y") == 1

    def test_find_cell_index_positional(self):
        assert _find_cell_index([{"id": "x"}], "cell-0") == 0

    def test_find_cell_index_unresolvable(self):
        assert _find_cell_index([{"id": "x"}], "zzz") is None

    def test_supports_cell_ids_45(self):
        assert _supports_cell_ids({"nbformat": 4, "nbformat_minor": 5}) is True

    def test_supports_cell_ids_old(self):
        assert _supports_cell_ids({"nbformat": 4, "nbformat_minor": 2}) is False
