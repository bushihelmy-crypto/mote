from __future__ import annotations

import os

import pytest

from mote.contracts.tools.effects import ToolEffect
from mote.product.toolsets.builtin.search import Search
from mote.product.toolsets.constants import GLIMPSE_EXTENSIONS
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.tool_result import ToolError

from .conftest import CapRole, bind, run, write_file


def _call(role, **kwargs):
    return run(bind(Search(), role).call(**kwargs))


@pytest.fixture
def tree(workspace):
    write_file(workspace / "a.py", "import os\ndef foo():\n    return ERROR ERROR\n")
    write_file(workspace / "b.py", "x = 1\nfoo()\n")
    write_file(workspace / "c.txt", "no match here\nERROR in text\n")
    sub = workspace / "sub"
    sub.mkdir()
    write_file(sub / "d.py", "def bar():\n    pass\n")
    vcs = workspace / ".git"
    vcs.mkdir()
    write_file(vcs / "config.py", "ERROR")
    return workspace


def test_requires_one_axis_or_cursor(workspace):
    with pytest.raises(ToolError, match="provide 'files'"):
        _call(CapRole(), path=str(workspace))


def test_files_and_content_are_composable(tree):
    role = CapRole(cwd=str(tree))

    result = _call(role, files="*.py", content="ERROR")

    assert "a.py:3" in result.output
    assert "c.txt" not in result.output
    assert result.data["summary"]["total_occurrences"] == 2
    assert [os.path.basename(path) for path in result.data["files"]] == ["a.py"]


def test_count_renders_occurrences_not_matching_lines(tree):
    result = _call(
        CapRole(cwd=str(tree)),
        content="ERROR",
        output_mode="count",
    )

    assert "a.py: 2" in result.output
    assert "c.txt: 1" in result.output
    assert "found 3 occurrences across 2 files" in result.output


def test_only_matching_and_context_render_from_structured_rows(tree):
    role = CapRole(cwd=str(tree))

    matching = _call(role, content="ERROR", output_mode="only_matching")
    contextual = _call(
        role,
        content="return ERROR",
        output_mode="content",
        context=1,
    )

    assert "[match] ERROR" in matching.output
    assert "[context] def foo():" in contextual.output


def test_name_results_are_stably_ordered_and_structured(tree):
    result = _call(CapRole(cwd=str(tree)), files="**/*.py")

    assert result.data["files"] == sorted(result.data["files"])
    assert all(os.path.isabs(path) for path in result.data["files"])
    assert "c.txt" not in result.output


def test_cursor_continues_immutable_artifact(tree):
    role = CapRole(cwd=str(tree))
    first = _call(
        role,
        content="ERROR",
        output_mode="only_matching",
        head_limit=1,
    )
    write_file(tree / "a.py", "replacement")

    second = _call(role, cursor=first.data["next_cursor"], head_limit=1)

    assert "[match] ERROR" in second.output
    assert second.data["result_artifact"] == first.data["result_artifact"]


def test_encoding_and_skipped_report_reach_tool_data(tree):
    target = tree / "legacy.txt"
    target.write_bytes("目标".encode("gbk"))
    binary = tree / "binary.dat"
    binary.write_bytes(b"\0TARGET")
    role = CapRole(cwd=str(tree))

    encoded = _call(role, path=str(target), content="目标", encoding="gbk")
    skipped = _call(role, path=str(binary), content="TARGET")

    assert encoded.data["summary"]["total_occurrences"] == 1
    assert skipped.data["skipped"][0]["reason"] == "binary"


def test_glimpses_use_structured_paths(tree):
    role = CapRole(cwd=str(tree))

    _call(role, content="ERROR")

    assert any(path.endswith("a.py") for path in role.glimpsed)
    assert not any(path.endswith("c.txt") for path in role.glimpsed)


@pytest.mark.parametrize("extension", GLIMPSE_EXTENSIONS)
def test_glimpses_all_registered_code_map_extensions(workspace, extension):
    target = workspace / f"target{extension}"
    role = CapRole(cwd=str(workspace))
    tool = bind(Search(), role)

    class _Path:
        display = str(target)

    class _Result:
        files = (_Path(),)

    tool._record_glimpses(_Result())

    assert role.glimpsed == [str(target)]


def test_invalid_regex_and_output_mode_are_tool_errors(tree):
    role = CapRole(cwd=str(tree))
    with pytest.raises(ToolError, match="invalid regular expression"):
        _call(role, content="[")
    with pytest.raises(ToolError, match="invalid output_mode"):
        _call(role, content="ERROR", output_mode="lines")


def test_declaration_is_read_only_and_single_line():
    assert Search.reconstructable is True
    assert Search.resolve_effect() is ToolEffect.PURE
    assert native_definition(Search).summary
    assert "\n" not in native_definition(Search).summary


class TestPermissionTarget:
    def test_relative_path_is_canonicalized_against_role_cwd(
        self,
        workspace,
        tmp_path,
    ):
        role_dir = tmp_path / "role_dir"
        role_dir.mkdir()
        tool = bind(Search(), CapRole(cwd=str(role_dir)))

        target = tool.permission_target({"path": "nested/.."})

        assert target == os.path.realpath(role_dir)
        assert target != os.path.realpath(workspace)

    def test_symlink_root_resolves_to_its_canonical_path(self, workspace):
        target = workspace / "target"
        target.mkdir()
        alias = workspace / "alias"
        alias.symlink_to(target, target_is_directory=True)
        tool = bind(Search(), CapRole(cwd=str(workspace)))

        assert tool.permission_target({"path": "alias"}) == os.path.realpath(target)

    def test_empty_path_targets_canonical_role_cwd(self, workspace):
        tool = bind(Search(), CapRole(cwd=str(workspace)))

        assert tool.permission_target({"path": ""}) == os.path.realpath(workspace)

    def test_cursor_continuation_has_no_live_filesystem_target(self, workspace):
        tool = bind(Search(), CapRole(cwd=str(workspace)))

        assert tool.permission_target({"path": str(workspace), "cursor": "cursor-token"}) == ""
