#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for langgraph-style state channels (:mod:`...bggraph.channels`).

Covers reducer detection (2-positional-arg callables, builtin guard), reducer
derivation from ``Annotated[T, reducer]`` metadata, and the merge semantics of
:func:`apply_updates` (last-value vs reducer-merged).
"""
from __future__ import annotations

import operator
from typing import Annotated

import pytest

from mote.executor.tasks.bggraph import GraphState
from mote.executor.tasks.bggraph.channels import _is_reducer, apply_updates, derive_reducers


class TestIsReducer:
    def test_two_arg_lambda_is_reducer(self):
        assert _is_reducer(lambda a, b: a + b)

    def test_operator_add_is_reducer(self):
        # operator.add introspects fine and exposes 2 positional params.
        assert _is_reducer(operator.add)

    def test_one_arg_callable_not_reducer(self):
        assert not _is_reducer(lambda a: a)

    def test_three_arg_callable_not_reducer(self):
        assert not _is_reducer(lambda a, b, c: a)

    def test_non_callable_not_reducer(self):
        assert not _is_reducer(42)
        assert not _is_reducer("nope")

    def test_non_introspectable_builtin_guarded(self):
        # Some C builtins raise from inspect.signature → treated as non-reducer.
        # ``len`` is 1-arg anyway; the guard is exercised by builtins whose
        # signature cannot be read. Use a class whose signature probe raises.
        class Weird:
            def __call__(self, *a):  # pragma: no cover - never invoked
                return a

        # A plain instance with *args is variadic, not 2 positional → not a reducer.
        assert not _is_reducer(Weird())


class TestDeriveReducers:
    def test_annotated_reducer_field_detected(self):
        class S(GraphState):
            items: Annotated[list, operator.add] = []
            name: str = ""

        reducers = derive_reducers(S)
        assert "items" in reducers
        assert reducers["items"] is operator.add
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
            items: Annotated[list, r1, r2] = []

        assert derive_reducers(S)["items"] is r1


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
            items: Annotated[list, operator.add] = []

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
            items: Annotated[list, merge] = []

        s = S()
        apply_updates(s, {"items": ["x"]}, derive_reducers(S))
        assert s.items == ["x"]

    def test_undeclared_key_lands_via_extra_allow(self):
        class S(GraphState):
            pass

        s = S()
        apply_updates(s, {"node_a": "result"}, {})
        assert s.node_a == "result"

    def test_empty_updates_noop(self):
        class S(GraphState):
            a: int = 0

        s = S(a=7)
        apply_updates(s, {}, {})
        assert s.a == 7
