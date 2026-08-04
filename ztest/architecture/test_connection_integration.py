from __future__ import annotations

from pathlib import Path

from mote.product.session_hosting.connection import ConnectionTimeoutPolicy

ROOT = Path(__file__).resolve().parents[2]


def test_surface_shutdown_paths_settle_connection_and_registry_owners() -> None:
    acp = (ROOT / "product/interfaces/acp/server.py").read_text(encoding="utf-8")
    agui = (ROOT / "product/interfaces/agui/server.py").read_text(encoding="utf-8")

    assert "await asyncio.gather(*tuple(self._inflight), return_exceptions=True)" in acp
    assert "await self._registry.aclose()" in acp
    assert "await app_[_REGISTRY_KEY].aclose()" in agui
    assert (
        "except Exception:\n            pass"
        not in acp[acp.index("async def serve_forever") : acp.index("def _decode")]
    )


def test_forced_connection_timeout_policy_retains_draining_owner() -> None:
    assert tuple(ConnectionTimeoutPolicy) == (ConnectionTimeoutPolicy.RETAIN_DRAINING,)
