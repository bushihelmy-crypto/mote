"""Generation-pinned aiohttp connection pools without credential state."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.client import ClientSession
from aiohttp.connector import TCPConnector
from aiohttp.resolver import DefaultResolver
from aiohttp.tracing import TraceConfig


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    fingerprint: str
    connection_limit: int = 100
    keepalive_seconds: float = 30.0
    verify_tls: bool = True
    allow_private_network: bool = False
    allowed_cidrs: tuple[str, ...] = ()
    allowed_dns_suffixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.fingerprint or self.connection_limit <= 0 or self.keepalive_seconds <= 0:
            raise ValueError("invalid connection configuration")
        if not self.verify_tls:
            raise ValueError("TLS verification cannot be disabled in the gateway transport")
        if not self.allow_private_network and (self.allowed_cidrs or self.allowed_dns_suffixes):
            raise ValueError("private allowlists require private-network opt-in")
        for cidr in self.allowed_cidrs:
            ipaddress.ip_network(cidr, strict=True)


class PolicyResolver(AbstractResolver):
    def __init__(self, config: ConnectionConfig) -> None:
        self._delegate = DefaultResolver()
        self._allow_private = config.allow_private_network
        self._networks = tuple(ipaddress.ip_network(value, strict=True) for value in config.allowed_cidrs)
        self._suffixes = tuple(value.lower().rstrip(".") for value in config.allowed_dns_suffixes)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        records = await self._delegate.resolve(host, port, family)
        hostname = host.lower().rstrip(".")
        suffix_allowed = any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in self._suffixes)
        if self._suffixes and not suffix_allowed:
            raise PermissionError("provider hostname is outside the egress allowlist")
        for record in records:
            address = ipaddress.ip_address(record["host"])
            if address.is_link_local or address.is_unspecified or address.is_multicast:
                raise PermissionError("provider DNS resolved to a forbidden address")
            if address in ipaddress.ip_network("169.254.169.254/32"):
                raise PermissionError("cloud metadata endpoint is forbidden")
            private = address.is_private or address.is_loopback or address.is_reserved
            cidr_allowed = any(address in network for network in self._networks)
            if private and not (self._allow_private and cidr_allowed):
                raise PermissionError("provider DNS resolved outside egress policy")
        return records

    async def close(self) -> None:
        await self._delegate.close()


@dataclass
class _PoolEntry:
    config: ConnectionConfig
    session: ClientSession
    references: int = 0


class AioHttpConnectionLease:
    def __init__(self, owner: "AioHttpConnectionPool", entry: _PoolEntry) -> None:
        self._owner = owner
        self._entry = entry
        self._released = False

    @property
    def session(self) -> ClientSession:
        if self._released:
            raise RuntimeError("connection lease already released")
        return self._entry.session

    async def release(self) -> None:
        if self._released:
            raise RuntimeError("connection lease already released")
        self._released = True
        await self._owner._release(self._entry.config.fingerprint)


class AioHttpConnectionPool:
    def __init__(self) -> None:
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def acquire(self, config: ConnectionConfig) -> AioHttpConnectionLease:
        async with self._lock:
            if self._closed:
                raise RuntimeError("connection pool is closed")
            entry = self._entries.get(config.fingerprint)
            if entry is None:
                entry = _PoolEntry(config=config, session=self._create_session(config))
                self._entries[config.fingerprint] = entry
            elif entry.config != config:
                raise ValueError("connection fingerprint reused with different policy")
            entry.references += 1
            return AioHttpConnectionLease(self, entry)

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._entries.values())
            self._entries.clear()
        await asyncio.gather(*(entry.session.close() for entry in entries))

    async def _release(self, fingerprint: str) -> None:
        async with self._lock:
            entry = self._entries.get(fingerprint)
            if entry is None or entry.references <= 0:
                raise RuntimeError("connection pool reference underflow")
            entry.references -= 1
            if entry.references:
                return
            del self._entries[fingerprint]
        await entry.session.close()

    @staticmethod
    def _create_session(config: ConnectionConfig) -> ClientSession:
        trace = TraceConfig()

        async def on_request_headers_sent(session, context, params) -> None:
            request_context = context.trace_request_ctx
            if not isinstance(request_context, dict) or "lifecycle" not in request_context:
                raise RuntimeError("gateway request missing lifecycle trace context")
            await request_context["lifecycle"].wire_started()

        trace.on_request_headers_sent.append(on_request_headers_sent)
        connector = TCPConnector(
            limit=config.connection_limit,
            keepalive_timeout=config.keepalive_seconds,
            ssl=True,
            resolver=PolicyResolver(config),
        )
        return ClientSession(
            connector=connector,
            trace_configs=[trace],
            auto_decompress=False,
            trust_env=False,
            raise_for_status=False,
        )
