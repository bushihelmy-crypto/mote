#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`TracingSubscriber` — span/LLM events -> a TracerBackend.

The subscriber rebuilds the trace tree from explicit IDs and drives a pluggable
backend; these drive it against a recording fake backend (no SDK). Covers span
lifecycle + parent threading, generation nesting under a started span, the
response/error update+end, the unmatched no-op, the ``trace_steps`` knob (step
spans skipped while root span + generations still trace), best-effort swallowing
of a backend exception, and the priority.
"""
from __future__ import annotations

import asyncio

from mote.common.events.types import LLMErrorEvent, LLMRequestEvent, LLMResponseEvent, SpanEndEvent, SpanStartEvent
from mote.common.observability.tracing import TracingSubscriber


def run(coro):
    return asyncio.run(coro)


# -- fake backend -----------------------------------------------------------
class _Handle:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.kw = kw
        self.updates: list = []
        self.ended = False


class _FakeBackend:
    def __init__(self):
        self.calls: list = []

    def start_span(self, *, span_id, parent_handle, trace_id, label, attributes):
        self.calls.append(("start_span", span_id, parent_handle, label))
        return _Handle("span", span_id=span_id, parent=parent_handle, label=label)

    def end_span(self, handle, *, status, error, attributes):
        handle.ended = True
        handle.status = status
        handle.error = error

    def start_generation(self, *, request_id, parent_handle, trace_id, model, input, metadata):
        self.calls.append(("start_generation", request_id, parent_handle, model))
        return _Handle("generation", request_id=request_id, parent=parent_handle, model=model)

    def update_generation(self, handle, **kw):
        handle.updates.append(kw)

    def end_generation(self, handle):
        handle.ended = True


def _sub(trace_steps=True):
    backend = _FakeBackend()
    return TracingSubscriber(backend, trace_steps=trace_steps), backend


# -- tests ------------------------------------------------------------------
def test_span_lifecycle_with_parent_threading():
    sub, backend = _sub()
    run(sub.handle(SpanStartEvent(span_id="s1", parent_span_id=None, trace_id="t", label="root")))
    run(sub.handle(SpanStartEvent(span_id="s2", parent_span_id="s1", trace_id="t", label="child")))

    root = sub._spans["s1"]
    child = sub._spans["s2"]
    assert root.kw["parent"] is None
    assert child.kw["parent"] is root  # threaded by handle, not ambient

    run(sub.handle(SpanEndEvent(span_id="s2", trace_id="t", status="ok")))
    assert child.ended is True
    assert "s2" not in sub._spans
    run(sub.handle(SpanEndEvent(span_id="s1", trace_id="t", status="error", error="x")))
    assert root.ended is True and root.status == "error" and root.error == "x"


def test_generation_nests_under_started_span():
    sub, backend = _sub()
    run(sub.handle(SpanStartEvent(span_id="s1", parent_span_id=None, trace_id="t", label="root")))
    run(
        sub.handle(
            LLMRequestEvent(
                request_id="r1",
                model="gpt-4o",
                provider="openai",
                messages=[{"role": "user", "content": "hi"}],
                parent_span_id="s1",
                trace_id="t",
            )
        )
    )
    gen = sub._gens["r1"]
    assert gen.kw["parent"] is sub._spans["s1"]
    assert gen.kw["model"] == "gpt-4o"

    run(
        sub.handle(
            LLMResponseEvent(
                request_id="r1", model="gpt-4o", content="hello", usage={"t": 1}, cost_usd=0.01, latency_ms=5.0
            )
        )
    )
    assert gen.ended is True
    assert gen.updates[-1]["output"] == "hello"
    assert gen.updates[-1]["metadata"]["cost_usd"] == 0.01
    assert "r1" not in sub._gens


def test_error_marks_and_ends_generation():
    sub, _ = _sub()
    run(sub.handle(LLMRequestEvent(request_id="r2", model="claude", provider="anthropic", messages=[])))
    gen = sub._gens["r2"]
    run(sub.handle(LLMErrorEvent(request_id="r2", model="claude", error_type="RateLimitError", error="429")))
    assert gen.ended is True
    assert gen.updates[-1]["level"] == "ERROR"
    assert "RateLimitError" in gen.updates[-1]["status_message"]
    assert sub._gens == {}


def test_unmatched_response_and_error_are_noops():
    sub, backend = _sub()
    run(sub.handle(LLMResponseEvent(request_id="ghost", model="m", content="x")))
    run(sub.handle(LLMErrorEvent(request_id="ghost", model="m", error_type="E", error="e")))
    run(sub.handle(SpanEndEvent(span_id="ghost", trace_id="t")))
    assert backend.calls == []  # nothing started


def test_trace_steps_off_skips_step_spans_but_keeps_root_and_generations():
    sub, backend = _sub(trace_steps=False)
    # Root span (no parent) still exports.
    run(sub.handle(SpanStartEvent(span_id="root", parent_span_id=None, trace_id="t", label="role.run")))
    # A nested step span (has a parent) is skipped.
    run(sub.handle(SpanStartEvent(span_id="step", parent_span_id="root", trace_id="t", label="think")))
    assert "root" in sub._spans
    assert "step" not in sub._spans

    # Generations still trace; a generation under the skipped step span just
    # gets a None parent handle (skipped span never stored).
    run(sub.handle(LLMRequestEvent(request_id="r", model="m", messages=[], parent_span_id="step")))
    gen = sub._gens["r"]
    assert gen.kw["parent"] is None

    # SpanEnd for the skipped step is a harmless no-op.
    run(sub.handle(SpanEndEvent(span_id="step", trace_id="t")))


def test_backend_exception_is_swallowed():
    class _Boom:
        def start_span(self, **kw):
            raise RuntimeError("backend down")

    sub = TracingSubscriber(_Boom(), trace_steps=True)
    out = run(sub.handle(SpanStartEvent(span_id="s", parent_span_id=None, trace_id="t", label="x")))
    assert out is None
    assert sub._spans == {}


def test_priority_is_85():
    assert TracingSubscriber.priority == 85
