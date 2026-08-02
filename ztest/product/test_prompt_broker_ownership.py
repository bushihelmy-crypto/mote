from __future__ import annotations

import pytest

from mote.product.session_hosting.prompt_broker import (
    PromptBroker,
    PromptHandle,
    PromptKind,
    PromptResolveDisposition,
    PromptScope,
)


def _scope(
    *, principal: str = "p1", thread: str = "t1", run: str = "r1", kind: PromptKind = PromptKind.APPROVAL
) -> PromptScope:
    return PromptScope(principal=principal, agent_id="agent", thread_id=thread, run_id=run, kind=kind)


@pytest.mark.asyncio
async def test_reply_requires_complete_owner_and_nonce() -> None:
    broker = PromptBroker()
    handle, future = broker.open(_scope(), ttl_seconds=30)
    foreign = PromptHandle(handle.prompt_id, handle.nonce, _scope(principal="p2"))
    assert broker.resolve(foreign, {"outcome": "accept"}) is PromptResolveDisposition.FOREIGN
    wrong_nonce = PromptHandle(handle.prompt_id, "wrong", handle.scope)
    assert broker.resolve(wrong_nonce, {"outcome": "accept"}) is PromptResolveDisposition.FOREIGN
    assert not future.done()


@pytest.mark.asyncio
async def test_question_cannot_resolve_approval_and_reply_is_once() -> None:
    broker = PromptBroker()
    handle, future = broker.open(_scope(), ttl_seconds=30)
    wrong_kind = PromptHandle(handle.prompt_id, handle.nonce, _scope(kind=PromptKind.QUESTION))
    assert broker.resolve(wrong_kind, {"answer": "yes"}) is PromptResolveDisposition.WRONG_KIND
    assert broker.resolve(handle, {"outcome": "accept"}) is PromptResolveDisposition.RESOLVED
    assert await future == {"outcome": "accept"}
    assert broker.resolve(handle, {"outcome": "reject"}) is PromptResolveDisposition.STALE


@pytest.mark.asyncio
async def test_scope_close_revokes_waiter() -> None:
    broker = PromptBroker()
    scope = _scope(run="closing")
    _, future = broker.open(scope, ttl_seconds=30)
    broker.cancel_scope(scope)
    assert future.cancelled()
