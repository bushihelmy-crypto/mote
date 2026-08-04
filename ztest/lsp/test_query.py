#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for Layer B LSP queries — documentSymbol / definition request path.

These exercise :class:`LspServerInstance.document_symbols` / ``.definition``
without a real language server by stubbing the endpoint's ``request`` (the same
seam :class:`JsonRpcEndpoint` exposes). We assert the request params are
LSP-shaped and that any failure degrades to ``[]`` (best-effort, never raises).
"""

from __future__ import annotations

import pytest

from mote.runtime.config.lsp import LspServerConfig
from mote.runtime.lsp.registry import DiagnosticRegistry
from mote.runtime.lsp.server import LspQueryError, LspServerInstance, path_to_uri

aio = pytest.mark.asyncio


class _FakeEndpoint:
    """Records requests + notifications; replies from a scripted result/exc."""

    def __init__(self, *, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict, timeout: float = 0.0):
        self.requests.append((method, params))
        if self._exc is not None:
            raise self._exc
        return self._result

    def notify(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))


def _instance(tmp_path, *, result=None, exc=None) -> tuple[LspServerInstance, _FakeEndpoint, str]:
    cfg = LspServerConfig(name="py", command=["true"], extensions=[".py"])
    inst = LspServerInstance(cfg, str(tmp_path), DiagnosticRegistry())
    ep = _FakeEndpoint(result=result, exc=exc)
    inst._endpoint = ep  # type: ignore[assignment]
    inst.alive = True
    # A real file so _ensure_open can read it.
    src = tmp_path / "m.py"
    src.write_text("x = 1\n", encoding="utf-8")
    return inst, ep, str(src)


@aio
async def test_document_symbols_opens_doc_and_sends_request(tmp_path):
    symbols = [{"name": "Foo", "kind": 5}, {"name": "bar", "kind": 12}]
    inst, ep, path = _instance(tmp_path, result=symbols)

    got = await inst.document_symbols(path)

    assert got == symbols
    # Opened the doc first (didOpen), then issued the documentSymbol request.
    assert ("textDocument/didOpen", ep.notifications[0][1]) == ep.notifications[0]
    assert len(ep.requests) == 1
    method, params = ep.requests[0]
    assert method == "textDocument/documentSymbol"
    assert params == {"textDocument": {"uri": path_to_uri(path)}}


@aio
async def test_document_symbols_non_list_reply_is_empty(tmp_path):
    inst, _ep, path = _instance(tmp_path, result={"not": "a list"})
    with pytest.raises(LspQueryError):
        await inst.document_symbols(path)


@aio
async def test_document_symbols_failure_yields_empty(tmp_path):
    inst, _ep, path = _instance(tmp_path, exc=RuntimeError("dead"))
    with pytest.raises(LspQueryError):
        await inst.document_symbols(path)


@aio
async def test_document_symbols_dead_server_no_request(tmp_path):
    inst, ep, path = _instance(tmp_path, result=[])
    inst.alive = False
    with pytest.raises(LspQueryError):
        await inst.document_symbols(path)
    assert ep.requests == []


@aio
async def test_definition_sends_position_and_normalizes_single_location(tmp_path):
    loc = {"uri": "file:///target.py", "range": {"start": {"line": 3, "character": 0}}}
    inst, ep, path = _instance(tmp_path, result=loc)

    got = await inst.definition(path, line=0, character=4)

    # A single Location dict is normalized to a one-element list.
    assert got == [loc]
    method, params = ep.requests[0]
    assert method == "textDocument/definition"
    assert params == {
        "textDocument": {"uri": path_to_uri(path)},
        "position": {"line": 0, "character": 4},
    }


@aio
async def test_definition_list_reply_passthrough(tmp_path):
    locs = [{"uri": "file:///a.py"}, {"uri": "file:///b.py"}]
    inst, _ep, path = _instance(tmp_path, result=locs)
    assert await inst.definition(path, 1, 2) == locs


@aio
async def test_definition_failure_yields_empty(tmp_path):
    inst, _ep, path = _instance(tmp_path, exc=TimeoutError())
    with pytest.raises(LspQueryError):
        await inst.definition(path, 0, 0)


@aio
async def test_definition_unreadable_path_no_request(tmp_path):
    inst, ep, _path = _instance(tmp_path, result=[])
    missing = str(tmp_path / "gone.py")
    with pytest.raises(LspQueryError):
        await inst.definition(missing, 0, 0)
    assert ep.requests == []


# -- F2: references request path ----------------------------------------------


@aio
async def test_references_sends_position_and_context(tmp_path):
    locs = [{"uri": "file:///a.py"}, {"uri": "file:///b.py"}]
    inst, ep, path = _instance(tmp_path, result=locs)

    got = await inst.references(path, line=2, character=4)

    assert got == locs
    method, params = ep.requests[0]
    assert method == "textDocument/references"
    assert params == {
        "textDocument": {"uri": path_to_uri(path)},
        "position": {"line": 2, "character": 4},
        "context": {"includeDeclaration": False},
    }


@aio
async def test_references_non_list_reply_is_empty(tmp_path):
    inst, _ep, path = _instance(tmp_path, result={"not": "a list"})
    with pytest.raises(LspQueryError):
        await inst.references(path, 0, 0)


@aio
async def test_references_failure_yields_empty(tmp_path):
    inst, _ep, path = _instance(tmp_path, exc=RuntimeError("dead"))
    with pytest.raises(LspQueryError):
        await inst.references(path, 0, 0)


@aio
async def test_references_dead_server_no_request(tmp_path):
    inst, ep, path = _instance(tmp_path, result=[])
    inst.alive = False
    with pytest.raises(LspQueryError):
        await inst.references(path, 0, 0)
    assert ep.requests == []


def test_references_capability_advertised(tmp_path):
    cfg = LspServerConfig(name="py", command=["true"], extensions=[".py"])
    inst = LspServerInstance(cfg, str(tmp_path), DiagnosticRegistry())
    params = inst._initialize_params()
    text_doc = params["capabilities"]["textDocument"]
    assert "references" in text_doc
