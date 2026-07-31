import asyncio

import pytest

from mote.runtime.models.generation_lifecycle import GenerationLifecycle


class _Resource:
    def __init__(self, name, events, *, fail_drain=False):
        self.name = name
        self.events = events
        self.fail_drain = fail_drain

    async def drain(self, *, timeout_seconds):
        self.events.append(("drain", self.name, timeout_seconds))
        if self.fail_drain:
            raise RuntimeError(self.name)

    async def aclose(self):
        self.events.append(("close", self.name))


def test_generation_lifecycle_drains_all_before_closing_any_resource():
    async def scenario():
        events = []
        first = _Resource("first", events)
        second = _Resource("second", events)
        lifecycle = GenerationLifecycle((first, second, first), drain_timeout_seconds=7)
        await lifecycle.aclose()
        await lifecycle.aclose()
        return events

    assert asyncio.run(scenario()) == [
        ("drain", "first", 7),
        ("drain", "second", 7),
        ("close", "first"),
        ("close", "second"),
    ]


def test_generation_lifecycle_closes_every_resource_after_drain_failure():
    async def scenario():
        events = []
        lifecycle = GenerationLifecycle(
            (
                _Resource("bad", events, fail_drain=True),
                _Resource("good", events),
            )
        )
        with pytest.raises(BaseExceptionGroup) as captured:
            await lifecycle.aclose()
        return events, captured.value

    events, error = asyncio.run(scenario())
    assert ("close", "bad") in events and ("close", "good") in events
    assert len(error.exceptions) == 1
