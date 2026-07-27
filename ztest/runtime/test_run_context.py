from __future__ import annotations

import asyncio

from mote.contracts.run_context import RunContext
from mote.runtime.run_context import bind_run_context, current_run_context


def test_binding_is_scoped_and_restores_the_parent_context() -> None:
    parent = RunContext(deps="parent", session_id="s1", run_id="r1")
    child = RunContext(deps="child", session_id="s2", run_id="r2")

    assert current_run_context() is None
    with bind_run_context(parent):
        assert current_run_context() is parent
        with bind_run_context(child):
            assert current_run_context() is child
        assert current_run_context() is parent
    assert current_run_context() is None


def test_binding_is_isolated_between_async_tasks() -> None:
    async def scenario() -> tuple[str, str]:
        one = RunContext(deps="one", session_id="s1", run_id="r1")
        two = RunContext(deps="two", session_id="s2", run_id="r2")
        ready = asyncio.Event()
        observed: dict[str, str] = {}

        async def observe(name: str, context: RunContext[str]) -> None:
            with bind_run_context(context):
                ready.set()
                await asyncio.sleep(0)
                active = current_run_context()
                assert active is not None
                observed[name] = active.deps

        await asyncio.gather(observe("one", one), observe("two", two))
        assert ready.is_set()
        assert current_run_context() is None
        return observed["one"], observed["two"]

    assert asyncio.run(scenario()) == ("one", "two")
