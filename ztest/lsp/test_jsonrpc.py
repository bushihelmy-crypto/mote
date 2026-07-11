#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.roles.lsp.jsonrpc`` — Content-Length framing + correlation.

Drives a JsonRpcEndpoint over an in-process subprocess running a tiny echo
responder, so request/response correlation, notification dispatch, and
close-fails-pending are all covered without mocking the transport.
"""
from __future__ import annotations

import asyncio
import sys

import pytest
from mote.roles.lsp.jsonrpc import JsonRpcEndpoint, _parse_content_length

aio = pytest.mark.asyncio

# A stdio responder: replies to request id with result {"echo": method}; on
# notification "ping" it emits a server notification "pong".
_RESPONDER = r"""
import sys, json
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: return None
        line = line.strip()
        if not line: break
        k,_,v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get(b"content-length", b"0"))
    if n <= 0: return None
    return json.loads(sys.stdin.buffer.read(n).decode())
def write(m):
    b = json.dumps(m).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
    sys.stdout.buffer.flush()
while True:
    msg = read()
    if msg is None: break
    if msg.get("id") is not None:
        if msg.get("method") == "boom":
            write({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-1,"message":"nope"}})
        else:
            write({"jsonrpc":"2.0","id":msg["id"],"result":{"echo":msg.get("method")}})
    elif msg.get("method") == "ping":
        write({"jsonrpc":"2.0","method":"pong","params":{"ok":True}})
"""


async def _spawn():
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _RESPONDER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return proc


def test_parse_content_length():
    assert _parse_content_length(b"Content-Length: 42\r\n\r\n") == 42
    assert _parse_content_length(b"X: y\r\n\r\n") is None
    assert _parse_content_length(b"Content-Length: notanint\r\n") is None


@aio
async def test_request_response_roundtrip():
    proc = await _spawn()
    ep = JsonRpcEndpoint(proc.stdin, proc.stdout)
    ep.start()
    try:
        result = await ep.request("hello", {}, timeout=5.0)
        assert result == {"echo": "hello"}
    finally:
        await ep.close()
        proc.kill()


@aio
async def test_error_response_raises():
    proc = await _spawn()
    ep = JsonRpcEndpoint(proc.stdin, proc.stdout)
    ep.start()
    try:
        with pytest.raises(RuntimeError):
            await ep.request("boom", {}, timeout=5.0)
    finally:
        await ep.close()
        proc.kill()


@aio
async def test_notification_dispatched():
    proc = await _spawn()
    got = []
    ep = JsonRpcEndpoint(proc.stdin, proc.stdout, on_notification=lambda m, p: got.append((m, p)))
    ep.start()
    try:
        ep.notify("ping", {})
        # Give the responder a beat to emit the server notification.
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.02)
        assert got and got[0][0] == "pong"
        assert got[0][1] == {"ok": True}
    finally:
        await ep.close()
        proc.kill()


@aio
async def test_request_after_close_raises():
    proc = await _spawn()
    ep = JsonRpcEndpoint(proc.stdin, proc.stdout)
    ep.start()
    await ep.close()
    proc.kill()
    with pytest.raises(ConnectionError):
        await ep.request("hello", {}, timeout=1.0)
