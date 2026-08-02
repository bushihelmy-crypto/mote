from pathlib import Path


def test_agui_reply_handler_requires_full_owner_binding() -> None:
    root = Path(__file__).parents[2]
    server = (root / "product/interfaces/agui/server.py").read_text(encoding="utf-8")
    broker = (root / "product/session_hosting/prompt_broker.py").read_text(encoding="utf-8")
    for field in ("promptNonce", "promptKind", "threadId", "runId", "agentId"):
        assert field in server
    assert 'request["mote.principal"]' in server
    assert "hmac.compare_digest" in broker
    assert "PromptResolveDisposition.WRONG_KIND" in broker
