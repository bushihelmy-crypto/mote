from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_adapter_has_no_anonymous_auth_downgrade() -> None:
    source = (ROOT / "product/config/adapters/mcp.py").read_text(encoding="utf-8")

    assert "loading without auth" not in source
    assert "ignoring it" not in source
    assert "McpConfigCompilationError" in source


def test_mcp_auth_is_preflighted_before_discovery_loop() -> None:
    source = (ROOT / "runtime/tools/mcp/universal.py").read_text(encoding="utf-8")

    preflight = source.index("self._auth_by_server = {")
    discovery = source.index("for server_config in servers:")
    assert preflight < discovery
