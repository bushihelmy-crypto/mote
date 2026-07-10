#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`FileRehydrator` — post-compaction working-set re-read.

The rehydrator re-reads the files a session most recently touched and turns
their *current* on-disk bytes into messages re-inserted after the summary. These
tests assert the ordering (most-recent-first), the budgets (per-file truncation,
whole-file drop over the total), and the best-effort skips (vanished /
unreadable files, a throwing provider).
"""
from __future__ import annotations

from metagpt.common.const import TOOL_CALLS
from metagpt.common.schema import AIMessage, UserMessage
from metagpt.context.compaction.rehydrate import FileRehydrator


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# empty / no-op
# ---------------------------------------------------------------------------


def test_no_provider_returns_empty():
    assert FileRehydrator(None).project() == []


def test_empty_trajectory_returns_empty():
    assert FileRehydrator(lambda: []).project() == []


def test_throwing_provider_is_swallowed():
    def boom():
        raise RuntimeError("nope")

    assert FileRehydrator(boom).project() == []


# ---------------------------------------------------------------------------
# reading + content
# ---------------------------------------------------------------------------


def test_reads_current_bytes(tmp_path):
    path = _write(tmp_path, "a.py", "print('hello')\n")
    msgs = FileRehydrator(lambda: [path]).project()
    assert len(msgs) == 1
    assert "print('hello')" in msgs[0].content
    assert path in msgs[0].content  # header carries the absolute path
    assert msgs[0].role == "user"


def test_reflects_latest_on_disk_not_stale(tmp_path):
    # Rehydration re-reads: the message must carry the *current* bytes, even if
    # the file changed since it was first read.
    path = _write(tmp_path, "a.py", "old")
    _write(tmp_path, "a.py", "new contents")
    (msg,) = FileRehydrator(lambda: [path]).project()
    assert "new contents" in msg.content
    assert "old" not in msg.content.split("\n\n", 1)[1]


# ---------------------------------------------------------------------------
# ordering + caps
# ---------------------------------------------------------------------------


def test_most_recent_first(tmp_path):
    # Trajectory is oldest→newest; rehydrator emits newest first.
    a = _write(tmp_path, "a.py", "AAA")
    b = _write(tmp_path, "b.py", "BBB")
    c = _write(tmp_path, "c.py", "CCC")
    msgs = FileRehydrator(lambda: [a, b, c]).project()
    assert [m.content.split("\n\n", 1)[1] for m in msgs] == ["CCC", "BBB", "AAA"]


def test_max_files_cap(tmp_path):
    paths = [_write(tmp_path, f"f{i}.py", f"body{i}") for i in range(5)]
    msgs = FileRehydrator(lambda: paths, max_files=2).project()
    assert len(msgs) == 2
    # newest two (f4, f3)
    bodies = [m.content.split("\n\n", 1)[1] for m in msgs]
    assert bodies == ["body4", "body3"]


def test_per_file_truncation(tmp_path):
    big = "\n".join(f"line {i}" for i in range(1000))
    path = _write(tmp_path, "big.py", big)
    (msg,) = FileRehydrator(lambda: [path], max_tokens_per_file=20).project()
    assert "truncated" in msg.content


def test_total_budget_drops_older_files(tmp_path):
    big = "\n".join(f"line {i}" for i in range(1000))
    a = _write(tmp_path, "a.py", big)  # oldest
    b = _write(tmp_path, "b.py", "small")  # newest
    # Budget only fits the small newest file; the big older one is dropped whole.
    msgs = FileRehydrator(
        lambda: [a, b], max_tokens_per_file=100_000, token_budget=20
    ).project()
    bodies = [m.content.split("\n\n", 1)[1] for m in msgs]
    assert bodies == ["small"]


# ---------------------------------------------------------------------------
# best-effort skips
# ---------------------------------------------------------------------------


def test_missing_file_skipped(tmp_path):
    present = _write(tmp_path, "here.py", "here")
    gone = str(tmp_path / "gone.py")
    msgs = FileRehydrator(lambda: [present, gone]).project()
    assert len(msgs) == 1
    assert "here" in msgs[0].content


def test_directory_path_skipped(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    present = _write(tmp_path, "f.py", "body")
    msgs = FileRehydrator(lambda: [present, str(d)]).project()
    assert len(msgs) == 1


# ---------------------------------------------------------------------------
# preserved-tail dedup (CC's collectReadToolFilePaths)
# ---------------------------------------------------------------------------


def _read_call(path: str) -> AIMessage:
    """An assistant turn that read ``path`` via the Read tool."""
    m = AIMessage(content="")
    m.add_metadata(TOOL_CALLS, [{"id": "c1", "name": "Read", "args": {"file_path": path}}])
    return m


def test_dedup_skips_file_shown_in_preserved_tail(tmp_path):
    a = _write(tmp_path, "a.py", "AAA")
    b = _write(tmp_path, "b.py", "BBB")
    # The tail already shows a.py via a Read call — rehydration must skip it.
    preserved = [_read_call(a)]
    msgs = FileRehydrator(lambda: [a, b]).project(preserved)
    bodies = [m.content.split("\n\n", 1)[1] for m in msgs]
    assert bodies == ["BBB"]  # only b.py, a.py deduped


def test_dedup_none_preserved_reads_all(tmp_path):
    a = _write(tmp_path, "a.py", "AAA")
    b = _write(tmp_path, "b.py", "BBB")
    # No preserved tail => no dedup => both re-read (newest first).
    msgs = FileRehydrator(lambda: [a, b]).project(None)
    bodies = [m.content.split("\n\n", 1)[1] for m in msgs]
    assert bodies == ["BBB", "AAA"]


def test_dedup_matches_relative_read_path(tmp_path, monkeypatch):
    # A Read call recorded a relative path; the trajectory stores the abspath.
    # Dedup must normalize both the same way (abspath + expanduser) and match.
    monkeypatch.chdir(tmp_path)
    abs_a = _write(tmp_path, "a.py", "AAA")
    preserved = [_read_call("a.py")]  # relative, resolved against cwd=tmp_path
    msgs = FileRehydrator(lambda: [abs_a]).project(preserved)
    assert msgs == []  # a.py deduped even though the tail used a relative path


def test_dedup_handles_json_string_args(tmp_path):
    # Recovery wire form stores args as a JSON string, not a dict.
    a = _write(tmp_path, "a.py", "AAA")
    m = AIMessage(content="")
    m.add_metadata(TOOL_CALLS, [{"id": "c1", "name": "Read", "args": f'{{"file_path": "{a}"}}'}])
    msgs = FileRehydrator(lambda: [a]).project([m])
    assert msgs == []  # deduped from the JSON-string args


def test_dedup_ignores_non_read_tool_calls(tmp_path):
    a = _write(tmp_path, "a.py", "AAA")
    m = AIMessage(content="")
    # A Bash call that mentions the path is NOT a Read — don't dedup on it.
    m.add_metadata(TOOL_CALLS, [{"id": "c1", "name": "Bash", "args": {"file_path": a}}])
    msgs = FileRehydrator(lambda: [a]).project([m])
    bodies = [msg.content.split("\n\n", 1)[1] for msg in msgs]
    assert bodies == ["AAA"]  # still re-read, Bash isn't a Read


def test_dedup_tolerates_plain_tail_messages(tmp_path):
    # Preserved tail with no tool_calls metadata must not break dedup.
    a = _write(tmp_path, "a.py", "AAA")
    preserved = [UserMessage(content="just some text"), AIMessage(content="reply")]
    msgs = FileRehydrator(lambda: [a]).project(preserved)
    bodies = [m.content.split("\n\n", 1)[1] for m in msgs]
    assert bodies == ["AAA"]  # nothing to dedup against
