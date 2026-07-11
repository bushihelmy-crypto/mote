"""Unit tests for the file filter (code_review/filter.py)."""
from __future__ import annotations

from mote.executor.tools.code_review.filter import should_review
from mote.executor.tools.code_review.parser import FileDiff, Hunk


def _file(path, *, is_binary=False, is_deleted=False, with_hunk=True):
    hunks = [Hunk(new_start=1, lines=[(1, "+x = 1")])] if with_hunk else []
    return FileDiff(path=path, is_binary=is_binary, is_deleted=is_deleted, hunks=hunks)


class TestSupportedExts:
    def test_python_accepted(self):
        assert should_review(_file("src/main.py")) is True

    def test_go_accepted(self):
        assert should_review(_file("cmd/server.go")) is True

    def test_unsupported_ext_rejected(self):
        assert should_review(_file("README.md")) is False
        assert should_review(_file("data.json")) is False
        assert should_review(_file("config.yaml")) is False

    def test_custom_ext_whitelist(self):
        assert should_review(_file("a.md"), supported_exts={".md"}) is True
        assert should_review(_file("a.py"), supported_exts={".md"}) is False


class TestSkips:
    def test_binary_skipped(self):
        assert should_review(_file("a.py", is_binary=True)) is False

    def test_deleted_skipped(self):
        assert should_review(_file("a.py", is_deleted=True)) is False

    def test_no_hunks_skipped(self):
        assert should_review(_file("a.py", with_hunk=False)) is False


class TestExcludeGlobs:
    def test_python_test_excluded(self):
        assert should_review(_file("test_foo.py")) is False
        assert should_review(_file("foo_test.py")) is False

    def test_go_test_excluded(self):
        assert should_review(_file("server_test.go")) is False

    def test_tests_dir_excluded(self):
        assert should_review(_file("pkg/tests/helper.py")) is False
        assert should_review(_file("a/ztest/x.py")) is False

    def test_vendored_excluded(self):
        assert should_review(_file("node_modules/lib/index.js")) is False
        assert should_review(_file("vendor/pkg/util.go")) is False

    def test_generated_excluded(self):
        assert should_review(_file("api.pb.go")) is False
        assert should_review(_file("proto_pb2.py")) is False
        assert should_review(_file("bundle.min.js")) is False

    def test_custom_exclude_override(self):
        # With an empty exclude list a test file is reviewed.
        assert should_review(_file("test_foo.py"), exclude_globs=[]) is True
