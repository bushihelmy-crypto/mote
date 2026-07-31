import asyncio
import socket

import pytest

from mote.product.models.transports.connections.aiohttp import ConnectionConfig, PolicyResolver


class _Resolver:
    def __init__(self, address):
        self.address = address

    async def resolve(self, host, port, family):
        return [
            {
                "hostname": host,
                "host": self.address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self):
        return None


class _MultiResolver:
    def __init__(self, *addresses):
        self.addresses = addresses

    async def resolve(self, host, port, family):
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
            for address in self.addresses
        ]

    async def close(self):
        return None


def test_connection_resolver_revalidates_public_and_private_addresses():
    async def scenario():
        public = PolicyResolver(ConnectionConfig(fingerprint="public"))
        public._delegate = _Resolver("93.184.216.34")
        assert await public.resolve("provider.example", 443, socket.AF_INET)

        blocked = PolicyResolver(ConnectionConfig(fingerprint="blocked"))
        blocked._delegate = _Resolver("127.0.0.1")
        with pytest.raises(PermissionError, match="egress policy"):
            await blocked.resolve("provider.example", 443, socket.AF_INET)

        allowed = PolicyResolver(
            ConnectionConfig(
                fingerprint="allowed",
                allow_private_network=True,
                allowed_cidrs=("10.0.0.0/8",),
            )
        )
        allowed._delegate = _Resolver("10.1.2.3")
        assert await allowed.resolve("internal.example", 443, socket.AF_INET)

        metadata = PolicyResolver(
            ConnectionConfig(
                fingerprint="metadata",
                allow_private_network=True,
                allowed_cidrs=("169.254.0.0/16",),
            )
        )
        metadata._delegate = _Resolver("169.254.169.254")
        with pytest.raises(PermissionError, match="forbidden"):
            await metadata.resolve("metadata.internal", 80, socket.AF_INET)

    asyncio.run(scenario())


def test_dns_allowlist_never_overrides_resolved_address_policy():
    async def scenario():
        suffix_only = PolicyResolver(
            ConnectionConfig(
                fingerprint="suffix-only",
                allow_private_network=True,
                allowed_dns_suffixes=("internal.example",),
            )
        )
        suffix_only._delegate = _Resolver("10.1.2.3")
        with pytest.raises(PermissionError, match="egress policy"):
            await suffix_only.resolve("api.internal.example", 443, socket.AF_INET)

        wrong_hostname = PolicyResolver(
            ConnectionConfig(
                fingerprint="wrong-hostname",
                allow_private_network=True,
                allowed_cidrs=("10.0.0.0/8",),
                allowed_dns_suffixes=("internal.example",),
            )
        )
        wrong_hostname._delegate = _Resolver("10.1.2.3")
        with pytest.raises(PermissionError, match="hostname"):
            await wrong_hostname.resolve("attacker.example", 443, socket.AF_INET)

        allowed = PolicyResolver(
            ConnectionConfig(
                fingerprint="both-allowed",
                allow_private_network=True,
                allowed_cidrs=("10.0.0.0/8",),
                allowed_dns_suffixes=("internal.example",),
            )
        )
        allowed._delegate = _Resolver("10.1.2.3")
        assert await allowed.resolve("api.internal.example", 443, socket.AF_INET)

    asyncio.run(scenario())


def test_mixed_dns_answers_fail_closed_when_any_address_is_forbidden():
    async def scenario():
        resolver = PolicyResolver(
            ConnectionConfig(
                fingerprint="mixed",
                allow_private_network=True,
                allowed_cidrs=("10.0.0.0/8",),
            )
        )
        resolver._delegate = _MultiResolver("10.1.2.3", "127.0.0.1")
        with pytest.raises(PermissionError, match="egress policy"):
            await resolver.resolve("internal.example", 443, socket.AF_INET)

    asyncio.run(scenario())
