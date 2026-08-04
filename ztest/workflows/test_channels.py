#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for langgraph-style state channels (:mod:`...bggraph.channels`).

Covers explicit reducer binding, reducer derivation from ``Annotated[T, Reducer]``
metadata, and the merge semantics of
:func:`apply_updates` (last-value vs reducer-merged).
"""

from __future__ import annotations

import operator
from typing import Annotated

import pytest

from mote.orchestration.workflows import GraphState, Reducer
from mote.orchestration.workflows.channels import apply_updates, derive_reducers


class TestDeriveReducers:
    def test_annotated_reducer_field_detected(self):
        class S(GraphState):
            items: Annotated[list, Reducer(operator.add)] = []
            name: str = ""

        reducers = derive_reducers(S)
        assert "items" in reducers
        assert reducers["items"].merge(["a"], ["b"]) == ["a", "b"]
        assert "name" not in reducers

    def test_plain_fields_have_no_reducer(self):
        class S(GraphState):
            a: int = 0
            b: str = ""

        assert derive_reducers(S) == {}

    def test_first_reducer_in_metadata_wins(self):
        r1 = lambda a, b: a + b  # noqa: E731
        r2 = lambda a, b: b  # noqa: E731

        class S(GraphState):
            items: Annotated[list, Reducer(r1), Reducer(r2)] = []

        assert derive_reducers(S)["items"].merge(["a"], ["b"]) == ["a", "b"]


class TestApplyUpdates:
    def test_last_value_plain_field(self):
        class S(GraphState):
            a: int = 0

        s = S(a=1)
        apply_updates(s, {"a": 5}, {})
        assert s.a == 5
        apply_updates(s, {"a": 9}, {})
        assert s.a == 9  # last write wins

    def test_reducer_merges(self):
        class S(GraphState):
            items: Annotated[list, Reducer(operator.add)] = []

        s = S()
        reducers = derive_reducers(S)
        apply_updates(s, {"items": [1]}, reducers)
        apply_updates(s, {"items": [2, 3]}, reducers)
        assert s.items == [1, 2, 3]  # operator.add appended both contributions

    def test_reducer_with_none_current(self):
        # current defaults to None when the field has no prior value.
        def merge(cur, upd):
            return (cur or []) + upd

        class S(GraphState):
            items: Annotated[list, Reducer(merge)] = []

        s = S()
        apply_updates(s, {"items": ["x"]}, derive_reducers(S))
        assert s.items == ["x"]

    def test_undeclared_key_is_rejected(self):
        class S(GraphState):
            pass

        s = S()
        with pytest.raises(ValueError, match="Object has no attribute"):
            apply_updates(s, {"node_a": "result"}, {})

    def test_empty_updates_noop(self):
        class S(GraphState):
            a: int = 0

        s = S(a=7)
        apply_updates(s, {}, {})
        assert s.a == 7
