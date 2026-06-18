"""Tests for :mod:`metagpt.executor.tasks.bggraph.base_node`."""

from __future__ import annotations

from typing import Annotated

import pytest

from metagpt.executor.tasks.bggraph import BaseNode, BgGraph, From, GraphState, Stage, START, END
from metagpt.executor.tasks.bggraph import GraphParamTypeError
from metagpt.executor.tasks.types import BgTaskResult

from .conftest import S


# ---------------------------------------------------------------------------
# Concrete test nodes
# ---------------------------------------------------------------------------


class Doubler(BaseNode):
    """Doubles the input value.

    Params:
        x: $input.x — the integer to double
    """

    name = "doubler"

    async def call(self, state: GraphState) -> Stage:
        async def submit():
            return state.x * 2  # type: ignore[attr-defined]

        return Stage(submit=submit())


class WithDescription(BaseNode):
    """Docstring fallback — should NOT be used."""

    name = "explicit"
    description = "Explicit description wins"

    async def call(self, state: GraphState) -> Stage:
        async def submit():
            return 42

        return Stage(submit=submit())


class DocstringOnly(BaseNode):
    """Auto-extracted description."""

    name = "doconly"

    async def call(self, state: GraphState) -> Stage:
        """Short desc from call.

        Params:
            val: $input.x — input
        """

        async def submit():
            return state.x  # type: ignore[attr-defined]

        return Stage(submit=submit())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


async def _run(graph: BgGraph, **inputs):
    res = await graph.compile()(**inputs)
    assert isinstance(res, BgTaskResult)
    return await res.poll_factory()


# ---------------------------------------------------------------------------
# get_description
# ---------------------------------------------------------------------------


def test_get_description_classvar_wins():
    assert WithDescription.get_description() == "Explicit description wins"


def test_get_description_docstring_fallback():
    assert DocstringOnly.get_description() == "Short desc from call."


# ---------------------------------------------------------------------------
# get_params
# ---------------------------------------------------------------------------


def test_get_params_auto():
    params = DocstringOnly.get_params()
    assert "val" in params
    assert params["val"]["from"] == "$input.x"
    assert params["val"]["desc"] == "input"


def test_get_params_class_docstring():
    # Params declared in the class docstring (not call) — for Doubler
    params = Doubler.get_params()
    # Doubler's call() has no Params section; class docstring is not scanned
    # via get_params (which only looks at call). So empty.
    assert params == {}


# ---------------------------------------------------------------------------
# Integration with BgGraph
# ---------------------------------------------------------------------------


async def test_base_node_class_in_graph():
    """Register a BaseNode subclass (type) and run the compiled graph."""
    g = BgGraph("test", state_schema=S)
    g.add_node("doubler", Doubler)
    g.add_edge(START, "doubler")
    g.add_edge("doubler", END)

    assert await _run(g, x=5) == 10


async def test_base_node_instance_in_graph():
    """Register a BaseNode instance and run."""
    g = BgGraph("test", state_schema=S)
    g.add_node("doubler", Doubler())
    g.add_edge(START, "doubler")
    g.add_edge("doubler", END)

    assert await _run(g, x=3) == 6


async def test_plain_function_backward_compat():
    """Plain functions still work (no regression)."""

    async def triple(state):
        """Triple the value.

        Params:
            x: $input.x — the value
        """

        async def submit():
            return state.x * 3

        return Stage(submit=submit())

    g = BgGraph("test", state_schema=S)
    g.add_node("triple", triple)
    g.add_edge(START, "triple")
    g.add_edge("triple", END)

    assert await _run(g, x=4) == 12


def test_stage_summary_uses_base_node_description():
    """BaseNode description appears in stage_summary."""
    g = BgGraph("test", state_schema=S)
    g.add_node("doubler", Doubler)
    g.add_edge(START, "doubler")
    g.add_edge("doubler", END)
    g.compile()  # validate
    summary = g.stage_summary
    assert "Doubles the input value." in summary


# ---------------------------------------------------------------------------
# Typed kwargs extraction from call() signature
# ---------------------------------------------------------------------------


class TypedKwargsNode(BaseNode):
    """Node that declares params via typed kwargs."""

    name = "typed_kwargs"

    async def call(self, state: GraphState, *, url: str, count: int = 0) -> Stage:
        """Do something.

        Params:
            url: $input.url — the endpoint URL
        """

        async def submit():
            return f"{url}:{count}"

        return Stage(submit=submit())


class KwargsOverrideNode(BaseNode):
    """Node where signature type overrides docstring type."""

    name = "kwargs_override"

    async def call(self, state: GraphState, *, val: int) -> Stage:
        """Process.

        Params:
            val: $input.x — str — wrong type in docstring
        """

        async def submit():
            return val * 2

        return Stage(submit=submit())


def test_typed_kwargs_extracted():
    """call(self, state, *, url: str, count: int) → get_params extracts both with types."""
    params = TypedKwargsNode.get_params()
    # url is from docstring AND signature
    assert "url" in params
    assert params["url"]["from"] == "$input.url"
    assert params["url"]["type"] is str  # signature overrides/confirms
    # count is only from signature (not in docstring)
    assert "count" in params
    assert params["count"]["type"] is int


def test_kwargs_override_docstring_type():
    """Signature type takes precedence over docstring type."""
    params = KwargsOverrideNode.get_params()
    assert "val" in params
    # Docstring says "str" but signature says int → int wins
    assert params["val"]["type"] is int


# ---------------------------------------------------------------------------
# Annotated[..., From(...)] source declaration on the signature
# ---------------------------------------------------------------------------


class FromMarkerNode(BaseNode):
    """Signature-only params declare their source via Annotated[..., From(...)]."""

    name = "from_marker"

    async def call(
        self,
        state: GraphState,
        *,
        val: Annotated[int, From("a")],
        prompt: Annotated[str, From("$input.text")],
    ) -> Stage:
        async def submit():
            return val

        return Stage(submit=submit())


class FromOverrideNode(BaseNode):
    """Annotated From overrides the docstring from; docstring desc is kept."""

    name = "from_override"

    async def call(self, state: GraphState, *, val: Annotated[int, From("b")]) -> Stage:
        """Process.

        Params:
            val: $input.x — str — described in docstring
        """

        async def submit():
            return val

        return Stage(submit=submit())


def test_from_marker_sets_source_for_signature_only_param():
    """A signature-only param gets its `from` from the Annotated From marker."""
    params = FromMarkerNode.get_params()
    assert params["val"]["from"] == "a"
    assert params["val"]["type"] is int
    assert params["val"]["desc"] == ""
    assert params["prompt"]["from"] == "$input.text"
    assert params["prompt"]["type"] is str


def test_from_marker_overrides_docstring_from_keeps_desc():
    """Non-empty Annotated From overrides docstring from; signature type wins; desc kept."""
    params = FromOverrideNode.get_params()
    assert params["val"]["from"] == "b"          # From("b") overrides $input.x
    assert params["val"]["type"] is int          # signature int over docstring str
    assert params["val"]["desc"] == "described in docstring"


# ---------------------------------------------------------------------------
# 3-segment docstring type parsing
# ---------------------------------------------------------------------------


class ThreeSegmentNode(BaseNode):
    """Node testing 3-segment param format."""

    name = "three_seg"

    async def call(self, state: GraphState) -> Stage:
        """Process.

        Params:
            audio: $input.url — str — raw audio URL
            text: tts.output — str — transcription result
        """

        async def submit():
            return "ok"

        return Stage(submit=submit())


def test_three_segment_docstring_type():
    """3-segment format: source — type — desc."""
    params = ThreeSegmentNode.get_params()
    assert params["audio"]["from"] == "$input.url"
    assert params["audio"]["type"] is str
    assert params["audio"]["desc"] == "raw audio URL"
    assert params["text"]["from"] == "tts.output"
    assert params["text"]["type"] is str
    assert params["text"]["desc"] == "transcription result"


# ---------------------------------------------------------------------------
# Runtime type validation
# ---------------------------------------------------------------------------


class TypedNode(BaseNode):
    """Node with typed param for runtime validation tests."""

    name = "typed"

    async def call(self, state: GraphState) -> Stage:
        """Process x.

        Params:
            x: $input.x — int — the value
        """

        async def submit():
            return state.x * 2  # type: ignore[attr-defined]

        return Stage(submit=submit())


async def test_runtime_type_mismatch_raises():
    """state.x = 'wrong' + param type int → GraphParamTypeError at runtime."""

    class StrState(GraphState):
        x: str = ""

    g = BgGraph("test", state_schema=StrState)
    # Bypass compile-time check by NOT declaring type in params
    # (or test via engine directly)
    g.add_node(
        "a",
        TypedNode,
        # Override params to avoid compile-time failure (StrState.x is str but type=int)
        params={"x": {"from": "$input.x", "desc": "val", "type": int}},
    )
    g.add_edge(START, "a")
    g.add_edge("a", END)
    # Compile will fail because StrState.x is str but type is int
    # So let's test directly at runtime with the correct state schema but wrong value
    # Better approach: use the original S (x:int) and pass a wrong value dynamically

    # Use a custom node that has type=int param but we force wrong state
    class BadState(GraphState):
        x: int = 0  # declared int but we'll set it to string at runtime

    g2 = BgGraph("test", state_schema=BadState)
    g2.add_node(
        "a",
        lambda state: None,  # placeholder — won't reach fn call
        params={"x": {"from": "$input.x", "desc": "val", "type": int}},
    )
    g2.add_edge(START, "a")
    g2.add_edge("a", END)
    executor = g2.compile()

    # Pydantic coerces "5" → 5 for int fields, but let's test with extra="allow"
    # by setting state directly
    from metagpt.executor.tasks.bggraph.engine import _validate_node_params_runtime

    state = BadState(x=0)
    object.__setattr__(state, "x", "not_an_int")  # bypass pydantic to force wrong type
    with pytest.raises(GraphParamTypeError, match="expected int, got str"):
        _validate_node_params_runtime(g2, "a", state)


async def test_runtime_type_ok_passes():
    """state.x = 42 + param type int → no error."""
    g = BgGraph("test", state_schema=S)
    g.add_node("typed", TypedNode)
    g.add_edge(START, "typed")
    g.add_edge("typed", END)

    res = await g.compile()(x=5)
    assert isinstance(res, BgTaskResult)
    result = await res.poll_factory()
    assert result == 10


async def test_runtime_none_value_skipped():
    """None values on state skip the isinstance check (not an error)."""
    from metagpt.executor.tasks.bggraph.engine import _validate_node_params_runtime

    class NullState(GraphState):
        x: int = 0

    g = BgGraph("test", state_schema=NullState)
    g.add_node(
        "a",
        lambda state: None,
        params={"x": {"from": "$input.x", "desc": "val", "type": int}},
    )
    g.add_edge(START, "a")
    g.add_edge("a", END)
    g.compile()

    state = NullState(x=0)
    object.__setattr__(state, "x", None)
    # Should NOT raise — None is skipped
    _validate_node_params_runtime(g, "a", state)


async def test_runtime_generic_type_validated():
    """TypeAdapter catches list[str] containing ints — deep generic validation."""
    from metagpt.executor.tasks.bggraph.engine import _validate_node_params_runtime

    class ListState(GraphState):
        items: list = []

    g = BgGraph("test", state_schema=ListState)
    g.add_node(
        "a",
        lambda state: None,
        params={"items": {"from": "$input.items", "desc": "val", "type": list[str]}},
    )
    g.add_edge(START, "a")
    g.add_edge("a", END)
    g.compile()

    state = ListState(items=["hello", 123])  # 123 is not str
    with pytest.raises(GraphParamTypeError, match="expected list, got list"):
        _validate_node_params_runtime(g, "a", state)
