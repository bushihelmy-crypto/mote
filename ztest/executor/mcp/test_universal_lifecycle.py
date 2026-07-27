from __future__ import annotations

import pytest

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
