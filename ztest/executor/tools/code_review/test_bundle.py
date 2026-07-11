"""Unit tests for related-file bundling (code_review/bundle.py).

``attach_related`` populates each FileDiff.related in place with sibling paths
worth reading for context, drawn from the whole changeset (tests included),
ordered test↔impl pair → same-stem sibling → same-dir co-change.
"""
from __future__ import annotations

from mote.executor.tools.code_review.bundle import attach_related
from mote.executor.tools.code_review.parser import FileDiff


def _f(path: str) -> FileDiff:
    return FileDiff(path=path)


def _attach(paths):
    files = [_f(p) for p in paths]
    attach_related(files)
    return {f.path: f.related for f in files}


class TestTestImplPairing:
    def test_test_prefix_pairs_with_impl(self):
        rel = _attach(["pkg/foo.py", "pkg/test_foo.py"])
        assert "pkg/test_foo.py" in rel["pkg/foo.py"]
        assert "pkg/foo.py" in rel["pkg/test_foo.py"]

    def test_go_suffix_pairs(self):
        rel = _attach(["h.go", "h_test.go"])
        assert "h_test.go" in rel["h.go"]
        assert "h.go" in rel["h_test.go"]

    def test_spec_suffix_pairs(self):
        rel = _attach(["x.ts", "x.spec.ts"])
        assert "x.spec.ts" in rel["x.ts"]

    def test_pairing_requires_same_dir(self):
        # test in a different directory does not pair.
        rel = _attach(["src/foo.py", "tests/test_foo.py"])
        assert "tests/test_foo.py" not in rel["src/foo.py"]


class TestSameStemSiblings:
    def test_header_pairs_with_source(self):
        rel = _attach(["a.c", "a.h"])
        assert "a.h" in rel["a.c"]
        assert "a.c" in rel["a.h"]


class TestSameDirCochange:
    def test_same_dir_files_related(self):
        rel = _attach(["pkg/a.py", "pkg/b.py"])
        assert "pkg/b.py" in rel["pkg/a.py"]
        assert "pkg/a.py" in rel["pkg/b.py"]

    def test_different_dir_not_related(self):
        rel = _attach(["one/a.py", "two/b.py"])
        assert rel["one/a.py"] == []
        assert rel["two/b.py"] == []


class TestOrderingAndCap:
    def test_pair_ranks_before_codir(self):
        # test_foo pairs first; an unrelated same-dir file ranks after.
        rel = _attach(["pkg/foo.py", "pkg/test_foo.py", "pkg/other.py"])
        order = rel["pkg/foo.py"]
        assert order.index("pkg/test_foo.py") < order.index("pkg/other.py")

    def test_no_self_reference(self):
        rel = _attach(["pkg/a.py", "pkg/b.py"])
        assert "pkg/a.py" not in rel["pkg/a.py"]

    def test_capped_at_max_related(self):
        paths = [f"pkg/f{i}.py" for i in range(10)]
        files = [_f(p) for p in paths]
        attach_related(files, max_related=3)
        for f in files:
            assert len(f.related) <= 3

    def test_no_duplicates(self):
        rel = _attach(["pkg/foo.py", "pkg/test_foo.py", "pkg/bar.py"])
        related = rel["pkg/foo.py"]
        assert len(related) == len(set(related))
