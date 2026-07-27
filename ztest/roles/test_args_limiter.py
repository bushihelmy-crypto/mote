"""Tests for the record-time action args limiter.

The limiter now does ONE thing: delegate to the executor's tool-agnostic
``persist_large_args`` (spill an oversized arg blob to disk losslessly, leaving a
``<persisted-output>`` pointer). It is invoked as ``(tool_name, args, call_id)``
and drops the (unused) tool name.

The Edit whole-file-write ``new_string`` fold is NO LONGER done here — it moved
to the compaction :class:`FoldReducer` (folded N rounds later, under the same
count/token gate as tool-result bodies). See ``test_fold.py`` for that path.
"""

from __future__ import annotations

from mote.runtime.agent.runtime_modules import build_args_limiter

# A large whole-file-write body. It must fall through to the lossless persist
# now (the marker-fold moved to compaction), so its contents are irrelevant.
_BIG_PY = ("def bar(a, b=2):\n    return a + b\n") * 200


class _FakeExecutor:
    """Minimal stand-in exposing only what the limiter reads now."""

    def __init__(self):
        self.persist_calls: list = []

    def persist_large_args(self, args, call_id):
        # a sentinel so tests can prove the lossless path was taken
        self.persist_calls.append((args, call_id))
        return {"_persisted": True, "call_id": call_id}


def _limiter():
    ex = _FakeExecutor()
    return build_args_limiter(ex), ex


def test_limiter_delegates_to_persist_dropping_tool_name():
    limit, ex = _limiter()
    out = limit("write", {"file_path": "x.py", "old_string": "", "new_string": _BIG_PY}, "id1")
    # No record-time marker fold: the whole-file write is passed VERBATIM to the
    # tool-agnostic persist (the >50k lossless backstop) — the marker-fold is the
    # compaction FoldReducer's job now.
    assert out.get("_persisted") is True
    assert len(ex.persist_calls) == 1
    args, call_id = ex.persist_calls[0]
    assert args["new_string"] == _BIG_PY  # unchanged — no eager fold
    assert call_id == "id1"


def test_substring_edit_delegates_to_persist():
    limit, ex = _limiter()
    out = limit("Edit", {"file_path": "x.py", "old_string": "foo", "new_string": _BIG_PY}, "id2")
    assert out.get("_persisted") is True
    assert len(ex.persist_calls) == 1


def test_non_edit_tool_delegates_to_persist():
    limit, ex = _limiter()
    out = limit("Bash", {"command": "ls"}, "id3")
    assert out.get("_persisted") is True
    assert ex.persist_calls[0][0] == {"command": "ls"}


def test_non_dict_args_delegates_to_persist():
    limit, ex = _limiter()
    out = limit("write", "raw-string-args", "id4")
    assert out.get("_persisted") is True
    assert ex.persist_calls[0][0] == "raw-string-args"
