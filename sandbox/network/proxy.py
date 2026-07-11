"""Local egress proxy — an asyncio HTTP/CONNECT proxy gated by the policy.

A minimal forward proxy bound to a random loopback port. The sandboxed command
is pointed at it via ``HTTP_PROXY``/``HTTPS_PROXY`` (see :mod:`.netns`).

Two request shapes are handled:

  * ``CONNECT host:port`` — the HTTPS tunnel handshake. We consult the policy on
    ``host``; on allow we open a raw TCP tunnel and splice bytes both ways
    (no MITM — we never see the TLS plaintext, only the SNI-equivalent host
    from the CONNECT line). On deny we return ``403`` and close.
  * ``GET http://host/...`` (absolute-form request line) — plain-HTTP forward
    proxying. We parse the host, consult the policy, and either forward the
    request verbatim to the origin and stream the response back, or ``403``.

P1 scope: no response rewriting, no header injection, no caching. The proxy's
only job is the allow/deny decision + byte relay.

SSRF / DNS-rebinding defence (two layers):

  * ``policy.allows`` gates the *host* — a literal IP in a private/loopback
    range is rejected outright; a hostname must match the allowlist.
  * but a hostname that passes the allowlist still has to be *resolved*, and a
    malicious (or attacker-influenced) allowlisted name can resolve to an
    internal address (``169.254.169.254``, ``127.0.0.1``, RFC1918…). And a
    plain ``open_connection(host, port)`` would resolve a *second* time —
    a TOCTOU window an attacker can exploit with a TTL=0 rebinding flip
    (validate a public IP, connect to a private one).

    So after the allowlist passes we resolve the host **once**, validate
    **every** returned address with the same SSRF table (:func:`is_blocked_host`),
    and connect to one of the *pinned* validated IPs — never re-resolving. One
    lookup, one validation, one connection: the rebinding window is closed.
    (TLS is unaffected: SNI + cert validation in the CONNECT tunnel are
    end-to-end between client and origin; pinning only fixes which IP we dial.)
"""
from __future__ import annotations

import asyncio
import socket
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

from mote.common.logs import logger
from mote.sandbox.network.policy import NetworkPolicy, is_blocked_host

_CRLF = b"\r\n"
_BUF = 65536

# A resolver maps ``(host, port)`` to a list of resolved IP-literal strings
# (IPv4 or IPv6, without brackets/port). Injectable so the resolve→validate→pin
# path is testable without real DNS.
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _default_resolver(host: str, port: int) -> list[str]:
    """Resolve *host* to its IP literals via the event loop's ``getaddrinfo``.

    Returns the de-duplicated address strings (order preserved). An empty list
    means the name did not resolve to any address.
    """
    loop = asyncio.get_event_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    seen: set[str] = set()
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = str(sockaddr[0])
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


class EgressProxy:
    """An asyncio forward proxy enforcing a :class:`NetworkPolicy`."""

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        host: str = "127.0.0.1",
        validate_resolved_ips: bool = True,
        resolver: Optional[Resolver] = None,
    ) -> None:
        self._policy = policy
        self._host = host
        # When True (default), resolve every allowed host and reject if ANY
        # resolved address is internal (SSRF via allowlisted name), then pin the
        # connection to a validated IP (no second resolution → no rebinding).
        # Tests that point the proxy at a loopback fake upstream pass False.
        self._validate_resolved_ips = validate_resolved_ips
        self._resolver: Resolver = resolver or _default_resolver
        self._server: Optional[asyncio.AbstractServer] = None
        self._port: int = 0

    @property
    def port(self) -> int:
        """The bound loopback port (0 until :meth:`start`)."""
        return self._port

    @property
    def url(self) -> str:
        """The ``http://host:port`` URL to inject as ``HTTP_PROXY``."""
        return f"http://{self._host}:{self._port}"

    async def start(self) -> None:
        """Bind to a random loopback port and start accepting connections."""
        self._server = await asyncio.start_server(self._handle, self._host, 0)
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        logger.debug(f"EgressProxy listening on {self.url}")

    async def shutdown(self) -> None:
        """Stop accepting and close the listening socket (idempotent)."""
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # --- request handling --------------------------------------------------

    async def _resolve_and_pin(self, host: str, port: int) -> Optional[str]:
        """Resolve *host*, validate every IP, return one pinned address.

        Returns the IP to dial (a validated literal) on success, or ``None`` if
        the host does not resolve or **any** resolved address is internal
        (fail-closed: a single bad address rejects the whole connection, so a
        rebinding set that mixes a public and a private record can't slip
        through). When :attr:`_validate_resolved_ips` is False the check is
        skipped and the original *host* is returned unchanged (test seam).
        """
        if not self._validate_resolved_ips:
            return host
        try:
            addrs = await self._resolver(host, port)
        except Exception as exc:  # noqa: BLE001 — DNS failure → deny, never crash
            logger.debug(f"EgressProxy resolve failed for {host!r}: {exc}")
            return None
        if not addrs:
            return None
        for ip in addrs:
            if is_blocked_host(ip):
                logger.debug(f"EgressProxy blocking {host!r}: resolved to internal address {ip}")
                return None
        # All resolved addresses are public; pin to the first validated one so
        # the actual connection does not trigger a second (re-bindable) lookup.
        return addrs[0]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.split()
            if len(parts) < 3:
                await self._reject(writer, 400, "Bad Request")
                return
            method, target = parts[0].decode("latin1"), parts[1].decode("latin1")

            if method.upper() == "CONNECT":
                await self._handle_connect(target, reader, writer)
            else:
                await self._handle_absolute(method, target, request_line, reader, writer)
        except Exception as exc:  # noqa: BLE001 — never let one client kill the proxy
            logger.debug(f"EgressProxy client error: {exc}")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_connect(self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a ``CONNECT host:port`` HTTPS tunnel request."""
        host, _, port_str = target.partition(":")
        port = int(port_str) if port_str.isdigit() else 443

        # Drain the rest of the CONNECT request headers (up to the blank line).
        await self._drain_headers(reader)

        if not self._policy.allows(host):
            await self._reject(writer, 403, "Forbidden by sandbox network policy")
            return

        dial_ip = await self._resolve_and_pin(host, port)
        if dial_ip is None:
            await self._reject(writer, 403, "Forbidden by sandbox network policy")
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(dial_ip, port)
        except Exception:  # noqa: BLE001
            await self._reject(writer, 502, "Bad Gateway")
            return

        writer.write(b"HTTP/1.1 200 Connection Established" + _CRLF + _CRLF)
        await writer.drain()

        await self._splice(reader, writer, up_reader, up_writer)

    async def _handle_absolute(
        self,
        method: str,
        target: str,
        request_line: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a plain-HTTP absolute-form request (``GET http://host/...``)."""
        split = urlsplit(target)
        host = split.hostname or ""
        port = split.port or 80

        if not self._policy.allows(host):
            # Still consume the request body's headers so the client gets a clean
            # response rather than a reset.
            await self._drain_headers(reader)
            await self._reject(writer, 403, "Forbidden by sandbox network policy")
            return

        dial_ip = await self._resolve_and_pin(host, port)
        if dial_ip is None:
            await self._drain_headers(reader)
            await self._reject(writer, 403, "Forbidden by sandbox network policy")
            return

        try:
            # Dial the pinned IP (no re-resolution). The forwarded ``Host``
            # header still carries the original hostname, so virtual-hosted
            # origins route correctly.
            up_reader, up_writer = await asyncio.open_connection(dial_ip, port)
        except Exception:  # noqa: BLE001
            await self._drain_headers(reader)
            await self._reject(writer, 502, "Bad Gateway")
            return

        # Rewrite the absolute-form request line to origin-form for the upstream.
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        origin_line = f"{method} {path} HTTP/1.1".encode("latin1") + _CRLF
        up_writer.write(origin_line)
        # Forward the remaining request headers + body verbatim.
        up_writer.write(await self._read_headers(reader))
        await up_writer.drain()

        await self._splice(reader, writer, up_reader, up_writer)

    # --- low-level helpers -------------------------------------------------

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> bytes:
        """Read header lines through the terminating blank line; return them."""
        out = bytearray()
        while True:
            line = await reader.readline()
            out += line
            if line in (_CRLF, b"\n", b""):
                break
        return bytes(out)

    @staticmethod
    async def _drain_headers(reader: asyncio.StreamReader) -> None:
        """Consume header lines through the terminating blank line, discarding."""
        while True:
            line = await reader.readline()
            if line in (_CRLF, b"\n", b""):
                break

    @staticmethod
    async def _reject(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        body = reason.encode("latin1")
        writer.write(
            f"HTTP/1.1 {code} {reason}".encode("latin1")
            + _CRLF
            + b"Content-Length: "
            + str(len(body)).encode()
            + _CRLF
            + b"Connection: close"
            + _CRLF
            + _CRLF
            + body
        )
        try:
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass

    async def _splice(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectionally relay bytes between client and upstream until EOF."""

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(_BUF)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(
            pump(c_reader, u_writer),
            pump(u_reader, c_writer),
        )


__all__ = ["EgressProxy"]
