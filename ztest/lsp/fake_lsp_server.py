#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A minimal fake LSP server for tests — speaks Content-Length JSON-RPC on stdio.

Behavior (just enough to exercise LspServerInstance/Service end-to-end):

- ``initialize`` -> replies with empty capabilities;
- ``initialized`` (notification) -> ignored;
- ``textDocument/didOpen`` / ``didChange`` / ``didSave`` -> publishes
  ``textDocument/publishDiagnostics``: if the document text contains the token
  ``ERROR`` it reports one error diagnostic; otherwise it clears diagnostics
  (empty list). This lets a test assert both "error appears" and "error resolved";
- ``shutdown`` -> replies null; ``exit`` (notification) -> process exits.

Pure stdlib so it can be spawned as ``python fake_lsp_server.py`` from a test.
"""
from __future__ import annotations

import json
import sys


def _read_message(stdin):
    """Read one Content-Length frame from a binary stdin; None at EOF."""
    headers = {}
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break  # blank line ends headers
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    if length <= 0:
        return None
    body = stdin.read(length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(stdout, message):
    body = json.dumps(message).encode("utf-8")
    stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stdout.write(body)
    stdout.flush()


def _publish(stdout, uri, text):
    if "ERROR" in text:
        diagnostics = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5},
                },
                "severity": 1,
                "message": "fake error token found",
                "source": "fake",
                "code": "E001",
            }
        ]
    else:
        diagnostics = []
    _write_message(
        stdout,
        {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diagnostics},
        },
    )


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        msg = _read_message(stdin)
        if msg is None:
            break
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            _write_message(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {}}})
        elif method in ("textDocument/didOpen", "textDocument/didChange", "textDocument/didSave"):
            doc = params.get("textDocument") or {}
            uri = doc.get("uri", "")
            # text is on the doc for didOpen, on params for didChange/didSave.
            text = doc.get("text")
            if text is None:
                changes = params.get("contentChanges") or []
                text = changes[0].get("text", "") if changes else params.get("text", "")
            _publish(stdout, uri, text or "")
        elif method == "shutdown":
            _write_message(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "exit":
            break


if __name__ == "__main__":
    main()
