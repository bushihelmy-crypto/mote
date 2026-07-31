from __future__ import annotations

import pytest

from mote.runtime.telemetry.observability.langfuse_integration import LangfuseRuntime


class _Client:
    def __init__(self) -> None:
        self.events: list[str] = []

    def flush(self) -> None:
        self.events.append("flush")

    def shutdown(self) -> None:
        self.events.append("shutdown")


@pytest.mark.asyncio
async def test_langfuse_runtime_flushes_before_shutdown_once() -> None:
    client = _Client()
    runtime = LangfuseRuntime(client, trace_steps=True)

    assert runtime.subscriber() is not None
    await runtime.aclose()
    await runtime.aclose()

    assert client.events == ["flush", "shutdown"]
    assert runtime.enabled is False
