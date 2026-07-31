#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the generic :mod:`mote.runtime.ledger.append_ledger` base.

Exercises the storage-agnostic mechanics via a tiny concrete subclass:
fold-to-latest-per-key on append, ``get``/``records`` reads, ``reap`` dropping
keys with an atomic rewrite (and a no-op for unknown keys), cross-instance
crash durability (a second instance over the same path sees prior writes), the
graceful skip of a torn/garbled line on load, and best-effort append that never
raises when the path is unwritable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from mote.runtime.ledger import AppendOnlyLedger, LedgerRecord


@dataclass(frozen=True)
class _FakeRecord:
    key: str
    value: str

    def to_json(self) -> str:
        return json.dumps({"key": self.key, "value": self.value}, ensure_ascii=False)


class _FakeLedger(AppendOnlyLedger[_FakeRecord]):
    def _parse_record(self, data: dict) -> _FakeRecord:
        return _FakeRecord(key=data["key"], value=data["value"])

    def _record_key(self, record: _FakeRecord) -> str:
        return record.key


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / "fake.jsonl"


@pytest.fixture
def ledger(path):
    return _FakeLedger(path)


class TestProtocol:
    def test_record_satisfies_protocol(self):
        assert isinstance(_FakeRecord("k", "v"), LedgerRecord)


class TestReads:
    def test_get_missing_is_none(self, ledger):
        assert ledger.get("nope") is None

    def test_records_empty_initially(self, ledger):
        assert ledger.records() == []

    def test_path_property(self, ledger, path):
        assert ledger.path == path


class TestAppendFold:
    def test_append_then_get(self, ledger):
        ledger.append(_FakeRecord("a", "1"))
        assert ledger.get("a") == _FakeRecord("a", "1")

    def test_latest_write_per_key_wins(self, ledger):
        ledger.append(_FakeRecord("a", "1"))
        ledger.append(_FakeRecord("a", "2"))
        assert ledger.get("a") == _FakeRecord("a", "2")

    def test_records_folds_to_latest(self, ledger):
        ledger.append(_FakeRecord("a", "1"))
        ledger.append(_FakeRecord("b", "1"))
        ledger.append(_FakeRecord("a", "2"))
        assert ledger.records() == [_FakeRecord("a", "2"), _FakeRecord("b", "1")]

    def test_records_preserves_insertion_order(self, ledger):
        for k in ("c", "a", "b"):
            ledger.append(_FakeRecord(k, "x"))
        assert [r.key for r in ledger.records()] == ["c", "a", "b"]

    def test_append_creates_parent_dirs(self, ledger, path):
        ledger.append(_FakeRecord("a", "1"))
        assert path.exists()


class TestDurability:
    def test_second_instance_sees_prior_writes(self, path):
        first = _FakeLedger(path)
        first.append(_FakeRecord("a", "1"))
        first.append(_FakeRecord("a", "2"))
        first.append(_FakeRecord("b", "9"))

        # A fresh instance (e.g. rebuilt in a new process after a crash) folds
        # the on-disk log back into its latest-per-key index.
        second = _FakeLedger(path)
        assert second.get("a") == _FakeRecord("a", "2")
        assert second.get("b") == _FakeRecord("b", "9")

    def test_load_skips_torn_line(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _FakeRecord("a", "1").to_json()
        # A half-written / garbled tail line must not break folding the rest.
        path.write_text(good + "\n" + "{not valid json" + "\n", encoding="utf-8")
        ledger = _FakeLedger(path)
        assert ledger.get("a") == _FakeRecord("a", "1")
        assert ledger.records() == [_FakeRecord("a", "1")]

    def test_load_skips_line_missing_key(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _FakeRecord("a", "1").to_json()
        missing = json.dumps({"value": "orphan"})  # no "key" → KeyError, skipped
        path.write_text(good + "\n" + missing + "\n", encoding="utf-8")
        ledger = _FakeLedger(path)
        assert ledger.records() == [_FakeRecord("a", "1")]

    def test_load_skips_blank_lines(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        good = _FakeRecord("a", "1").to_json()
        path.write_text("\n" + good + "\n\n", encoding="utf-8")
        ledger = _FakeLedger(path)
        assert ledger.records() == [_FakeRecord("a", "1")]

    def test_missing_file_loads_empty(self, path):
        assert not path.exists()
        assert _FakeLedger(path).records() == []


class TestReap:
    def test_reap_drops_key(self, ledger):
        ledger.append(_FakeRecord("a", "1"))
        ledger.append(_FakeRecord("b", "1"))
        ledger.reap(["a"])
        assert ledger.get("a") is None
        assert ledger.get("b") == _FakeRecord("b", "1")

    def test_reap_rewrites_file(self, ledger, path):
        ledger.append(_FakeRecord("a", "1"))
        ledger.append(_FakeRecord("b", "1"))
        ledger.reap(["a"])
        # The atomically-rewritten file no longer carries the reaped key, so a
        # fresh instance does not resurrect it.
        assert _FakeLedger(path).get("a") is None
        assert _FakeLedger(path).get("b") == _FakeRecord("b", "1")

    def test_reap_unknown_key_is_noop(self, ledger, path):
        ledger.append(_FakeRecord("a", "1"))
        before = path.read_text(encoding="utf-8")
        ledger.reap(["nonexistent"])
        assert ledger.get("a") == _FakeRecord("a", "1")
        # No rewrite when nothing was removed.
        assert path.read_text(encoding="utf-8") == before

    def test_reap_empty_iterable_is_noop(self, ledger):
        ledger.append(_FakeRecord("a", "1"))
        ledger.reap([])
        assert ledger.get("a") == _FakeRecord("a", "1")

    def test_reap_multiple_keys(self, ledger):
        for k in ("a", "b", "c"):
            ledger.append(_FakeRecord(k, "x"))
        ledger.reap(["a", "c"])
        assert [r.key for r in ledger.records()] == ["b"]


class TestBestEffort:
    def test_append_never_raises_when_unwritable(self, tmp_path, monkeypatch):
        # Force the durable write to fail; the in-memory index must still update
        # and the caller must never see an exception.
        from mote.runtime import persistence as disk_mod

        def boom(*args, **kwargs):
            raise OSError("disk full")

        ledger = _FakeLedger(tmp_path / "x.jsonl")
        monkeypatch.setattr(disk_mod.disk_io, "append_line", boom)
        ledger.append(_FakeRecord("a", "1"))  # no raise
        assert ledger.get("a") == _FakeRecord("a", "1")

    def test_reap_never_raises_when_rewrite_fails(self, tmp_path, monkeypatch):
        from mote.runtime import persistence as disk_mod

        ledger = _FakeLedger(tmp_path / "x.jsonl")
        ledger.append(_FakeRecord("a", "1"))
        ledger.append(_FakeRecord("b", "1"))

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(disk_mod.disk_io, "atomic_write", boom)
        ledger.reap(["a"])  # no raise
        assert ledger.get("a") is None  # in-memory index still updated
