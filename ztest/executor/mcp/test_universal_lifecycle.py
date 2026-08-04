from __future__ import annotations

import pytest

from mote.runtime.tools.mcp.lifecycle import McpCandidate, McpCleanupDisposition, McpLifecycle, McpLifecycleState
from mote.runtime.tools.mcp.toolsets import XmlMcpToolset
from mote.runtime.tools.mcp.universal import UniversalMCP


class _Client:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.attempts = 0

    async def __aexit__(self, *_exc) -> None:
        self.attempts += 1
        if self.fail_once and self.attempts == 1:
            raise RuntimeError("transient close")


@pytest.mark.asyncio
async def test_cleanup_retains_only_failed_mcp_clients_for_retry() -> None:
    owner = UniversalMCP()
    retrying = _Client(fail_once=True)
    healthy = _Client()
    owner.clients = {"retrying": retrying, "healthy": healthy}

    with pytest.raises(RuntimeError, match="retrying.*transient close"):
        await owner.cleanup_clients()
    assert owner.clients == {"retrying": retrying}
    assert healthy.attempts == 1

    await owner.cleanup_clients()
    assert owner.clients == {}
    assert retrying.attempts == 2


@pytest.mark.asyncio
async def test_generation_activation_and_prior_cleanup_are_explicit() -> None:
    lifecycle = McpLifecycle()
    owner = UniversalMCP()
    candidate = McpCandidate(1, owner, XmlMcpToolset(owner), ())
    assert lifecycle.activate(candidate) is None
    assert lifecycle.generation == 1
    assert lifecycle.state is McpLifecycleState.ACTIVE

    receipt = await lifecycle.settle_prior(None, generation=0)
    assert receipt.disposition is McpCleanupDisposition.SETTLED
    await lifecycle.teardown()
    assert lifecycle.state is McpLifecycleState.EMPTY


@pytest.mark.asyncio
async def test_failed_prior_cleanup_blocks_following_generation() -> None:
    lifecycle = McpLifecycle()
    owner = UniversalMCP()
    owner.clients = {"broken": _Client(fail_once=True)}
    current = UniversalMCP()
    lifecycle.activate(McpCandidate(1, current, XmlMcpToolset(current), ()))

    receipt = await lifecycle.settle_prior(owner, generation=0)
    assert receipt.disposition is McpCleanupDisposition.CLEANUP_FAILED
    assert lifecycle.state is McpLifecycleState.DRAINING
    with pytest.raises(RuntimeError, match="draining"):
        following = UniversalMCP()
        lifecycle.activate(McpCandidate(2, following, XmlMcpToolset(following), ()))

    recovered = await lifecycle.settle_prior(owner, generation=0)
    assert recovered.disposition is McpCleanupDisposition.SETTLED
    assert lifecycle.state is McpLifecycleState.ACTIVE
