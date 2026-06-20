#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`LangfuseBackend` — the handle-threaded langfuse mapping.

Langfuse itself need not be installed: the backend resolves its client through
an injectable ``client_factory``, so a fake client/handle records the exact API
surface used. Locks the explicit-parent nesting (child created off the parent
*handle*, root off the client), the session propagation on the root, the
generation update/end mapping, and the best-effort swallowing of failures.
"""
from __future__ import annotations

from metagpt.common.observability.langfuse_backend import LangfuseBackend


# -- fakes ------------------------------------------------------------------
class _FakeObservation:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.kw = kw
        self.children: list = []
        self.updates: list = []
        self.ended = False

    def start_observation(self, **kwargs):
        child = _FakeObservation(kwargs.get("as_type"), **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True


class _FakeClient:
    def __init__(self):
        self.roots: list = []
        self.session_ids: list = []

    def start_observation(self, **kwargs):
        obs = _FakeObservation(kwargs.get("as_type"), **kwargs)
        self.roots.append(obs)
        return obs

    def update_current_trace(self, *, session_id):
        self.session_ids.append(session_id)


def _backend():
    client = _FakeClient()
    return LangfuseBackend(client_factory=lambda: client), client


# -- tests ------------------------------------------------------------------
def test_root_span_created_off_client_and_sets_session():
    backend, client = _backend()
    handle = backend.start_span(span_id="s1", parent_handle=None, trace_id="sess-1", label="root", attributes={})
    assert handle is client.roots[0]
    assert handle.kw["as_type"] == "span"
    assert handle.kw["name"] == "root"
    assert client.session_ids == ["sess-1"]


def test_child_span_created_off_parent_handle():
    backend, client = _backend()
    root = backend.start_span(span_id="s1", parent_handle=None, trace_id="t", label="root", attributes={})
    child = backend.start_span(span_id="s2", parent_handle=root, trace_id="t", label="child", attributes={})
    assert child in root.children
    assert child.kw["as_type"] == "span"
    # No new root observation created off the client for the child.
    assert len(client.roots) == 1


def test_generation_child_of_span_handle():
    backend, client = _backend()
    root = backend.start_span(span_id="s1", parent_handle=None, trace_id="t", label="root", attributes={})
    gen = backend.start_generation(
        request_id="r1", parent_handle=root, trace_id="t", model="gpt-4o", input=[{"x": 1}], metadata={"provider": "openai"}
    )
    assert gen in root.children
    assert gen.kw["as_type"] == "generation"
    assert gen.kw["model"] == "gpt-4o"
    assert gen.kw["name"] == "llm:gpt-4o"


def test_generation_off_client_when_no_parent():
    backend, client = _backend()
    gen = backend.start_generation(
        request_id="r1", parent_handle=None, trace_id="t", model="m", input=[], metadata={}
    )
    assert gen is client.roots[0]


def test_update_and_end_generation_map_through():
    backend, _ = _backend()
    gen = backend.start_generation(request_id="r", parent_handle=None, trace_id="t", model="m", input=[], metadata={})
    backend.update_generation(gen, output="hi", usage={"t": 1}, metadata={"cost_usd": 0.01})
    backend.end_generation(gen)
    assert gen.updates[-1] == {"output": "hi", "usage": {"t": 1}, "metadata": {"cost_usd": 0.01}}
    assert gen.ended is True


def test_end_span_error_status_records_level():
    backend, _ = _backend()
    span = backend.start_span(span_id="s", parent_handle=None, trace_id="t", label="x", attributes={})
    backend.end_span(span, status="error", error="boom", attributes={})
    assert any(u.get("level") == "ERROR" for u in span.updates)
    assert span.ended is True


def test_none_handles_are_noops():
    backend, _ = _backend()
    # None handle (a prior start failed) degrades to a silent no-op.
    backend.end_span(None, status="ok", error="", attributes={})
    backend.update_generation(None, output="x")
    backend.end_generation(None)


def test_client_failure_is_swallowed():
    class _Boom:
        def start_observation(self, **kw):
            raise RuntimeError("langfuse down")

    backend = LangfuseBackend(client_factory=lambda: _Boom())
    handle = backend.start_span(span_id="s", parent_handle=None, trace_id="t", label="x", attributes={})
    assert handle is None  # degrades to None, never raises
