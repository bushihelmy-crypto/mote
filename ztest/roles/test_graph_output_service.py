"""Graph output service owns Graph terminal semantics behind the capability seam."""

from __future__ import annotations

import pytest

from mote.contracts.output import GraphOutputContractSpec, RunKind
from mote.contracts.task.graph_errors import GraphError
from mote.runtime.output.graph_service import GraphOutputService


class _Lease:
    fencing_token = 7

    def assert_current(self, run_id: str, fencing_token: int) -> None:
        assert run_id == "graph-1"
        assert fencing_token == self.fencing_token

    def guard(self, run_id: str, fencing_token: int):
        from contextlib import nullcontext

        self.assert_current(run_id, fencing_token)
        return nullcontext()


def _spec() -> GraphOutputContractSpec:
    return GraphOutputContractSpec(
        namespace="test",
        name="graph-result",
        version="1",
        schema_={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )


async def _drain_writes() -> None:
    return None


def _service() -> GraphOutputService:
    return GraphOutputService(
        take_restore=lambda _run_id: None,
        current_lease=lambda _run_id: _Lease(),
        drain_writes=_drain_writes,
    )


@pytest.mark.asyncio
async def test_finalize_validates_and_commits_as_graph_output():
    service = _service()

    committed = await service.finalize(output={"count": 2}, contract_spec=_spec(), run_id="graph-1")

    assert committed.value == {"count": 2}
    assert committed.run_kind is RunKind.GRAPH
    assert committed.fencing_token == 7


@pytest.mark.asyncio
async def test_finalize_reports_normalized_contract_issues():
    service = _service()

    with pytest.raises(GraphError) as caught:
        await service.finalize(output={"count": "bad"}, contract_spec=_spec(), run_id="graph-1")

    assert caught.value.context["issues"][0]["path"] == ["count"]
    assert caught.value.context["issues"][0]["code"] == "type"


@pytest.mark.asyncio
async def test_resume_without_restored_output_is_inert():
    service = _service()

    assert await service.resume(contract_spec=_spec(), run_id="graph-1") is None
