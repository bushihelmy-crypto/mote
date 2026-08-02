from __future__ import annotations

import json

import pytest

from mote.product.config.adapters.mcp import McpConfigCompilationError, load_mcp_servers
from mote.product.extensions.sources import ExtensionKind, ExtensionSourcePolicy
from mote.runtime.tools.mcp.oauth import McpAuthenticationConfigurationError
from mote.runtime.tools.mcp.universal import UniversalMCP


def _source(tmp_path, servers: dict):
    user_root = tmp_path / "user"
    user_root.mkdir()
    path = user_root / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    policy = ExtensionSourcePolicy(user_root=user_root, builtin_roots=())
    return policy.inspect(ExtensionKind.MCP, path)


def test_absent_oauth_remains_explicitly_anonymous(tmp_path) -> None:
    source = _source(tmp_path, {"public": {"url": "https://example.test/sse"}})

    servers = load_mcp_servers((source,))

    assert len(servers) == 1
    assert servers[0].oauth is None


@pytest.mark.parametrize("oauth", [None, "invalid", {}, {"scopes": ["read"]}])
def test_declared_invalid_oauth_rejects_whole_candidate(tmp_path, oauth) -> None:
    source = _source(
        tmp_path,
        {
            "public": {"url": "https://public.test/sse"},
            "secured": {"url": "https://secured.test/sse", "oauth": oauth},
        },
    )

    with pytest.raises(McpConfigCompilationError) as raised:
        load_mcp_servers((source,))

    assert raised.value.source == source.canonical_path
    assert raised.value.server_name == "secured"


@pytest.mark.asyncio
async def test_missing_oauth_storage_aborts_before_client_construction(tmp_path, monkeypatch) -> None:
    source = _source(
        tmp_path,
        {
            "secured": {
                "url": "https://secured.test/sse",
                "oauth": {"token_url": "https://issuer.test/token"},
            }
        },
    )
    servers = load_mcp_servers((source,))
    owner = UniversalMCP(servers=servers, oauth_root=None)
    calls = 0

    def unexpected_client(_server):
        nonlocal calls
        calls += 1
        raise AssertionError("client must not be built")

    monkeypatch.setattr(owner, "_build_client", unexpected_client)

    with pytest.raises(McpAuthenticationConfigurationError, match="storage root"):
        await owner.initialize()

    assert calls == 0
    assert owner.discovered_tools() == ()
