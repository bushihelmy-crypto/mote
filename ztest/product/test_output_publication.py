from __future__ import annotations

import json

import pytest

from mote.contracts.conversation import AIMessage
from mote.contracts.output.publication import OutputPublicationDisposition, OutputPublicationRequest
from mote.product.agents.output_publication import DurableOutputPublisher


class Routing:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.messages = []

    def set_addresses(self, agent_id, addresses):
        return None

    def publish_message(self, message):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("offline")
        self.messages.append(message)


def request() -> OutputPublicationRequest:
    return OutputPublicationRequest(
        publication_id="output:agent:run-1",
        source_agent_id="agent",
        candidate_id="candidate",
        contract_id="mote.text@1",
        run_id="run-1",
        run_kind="agent",
        message=AIMessage(content="done", send_to={"user"}),
    )


@pytest.mark.asyncio
async def test_accept_is_durable_before_routing_and_restart_reconciles(tmp_path):
    path = tmp_path / "output-publications.json"
    first = DurableOutputPublisher(path, None)

    receipt = await first.accept(request())

    assert receipt.disposition is OutputPublicationDisposition.ACCEPTED
    assert json.loads(path.read_text())["records"][0]["state"] == "pending"

    routing = Routing()
    recovered = DurableOutputPublisher(path, routing)
    assert await recovered.reconcile_once() is True
    assert [message.content for message in routing.messages] == ["done"]
    assert json.loads(path.read_text())["records"][0]["state"] == "acked"


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_dead_letters_after_bound(tmp_path):
    path = tmp_path / "output-publications.json"
    routing = Routing(failures=5)
    publisher = DurableOutputPublisher(path, routing)
    publication = request()
    await publisher.accept(publication)

    duplicate = await publisher.accept(publication)
    assert duplicate.disposition is OutputPublicationDisposition.ALREADY_ACCEPTED
    for _ in range(5):
        assert await publisher.reconcile_once() is False

    record = json.loads(path.read_text())["records"][0]
    assert record["state"] == "dead_letter"
    assert record["attempts"] == 5
