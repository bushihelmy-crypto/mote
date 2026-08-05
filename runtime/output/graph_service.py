"""Graph-specific terminal output validation, commit, and recovery."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from mote.contracts.conversation import AIMessage, CauseBy
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.model.turn import FinalCandidateAction
from mote.contracts.output import CommittedOutput, GraphOutputContractSpec, RunKind
from mote.contracts.ports.execution.commit_fence import CommitFence
from mote.contracts.ports.session.facts import SessionFactSink
from mote.contracts.task.graph_errors import GraphError
from mote.kernel.output import OutputContract
from mote.runtime.output.engine import OutputEngine


class GraphOutputLease(CommitFence, Protocol):
    @property
    def fencing_token(self) -> int: ...


class GraphOutputService:
    """Own Graph terminal-output semantics without pretending Graph is an Agent run."""

    def __init__(
        self,
        *,
        take_restore: Callable[[str], dict | None],
        current_lease: Callable[[str], GraphOutputLease],
        drain_writes: Callable[[], Awaitable[None]],
        session_fact_sink: SessionFactSink | None = None,
    ) -> None:
        self._take_restore = take_restore
        self._current_lease = current_lease
        self._drain_writes = drain_writes
        self._session_fact_sink = session_fact_sink

    @staticmethod
    def _contract(contract_spec: GraphOutputContractSpec) -> OutputContract[JsonValue]:
        return OutputContract.from_json_schema(
            contract_spec.schema_,
            namespace=contract_spec.namespace,
            name=contract_spec.name,
            version=contract_spec.version,
        )

    def _engine(
        self,
        *,
        contract_spec: GraphOutputContractSpec,
        run_id: str,
        restored_state: dict | None = None,
    ) -> OutputEngine[JsonValue]:
        lease = self._current_lease(run_id)
        return OutputEngine(
            self._contract(contract_spec),
            restored_state=restored_state,
            run_id=run_id,
            run_kind=RunKind.GRAPH,
            commit_fence=lease,
            fencing_token=lease.fencing_token,
            drain_writes=self._drain_writes,
            session_fact_sink=self._session_fact_sink,
        )

    async def finalize(
        self, *, output: JsonValue, contract_spec: GraphOutputContractSpec, run_id: str
    ) -> CommittedOutput[JsonValue]:
        """Validate and commit one model-authored Graph terminal value."""
        engine = self._engine(contract_spec=contract_spec, run_id=run_id)
        frozen_output = freeze_json(output, path="graph_output")
        evaluation = await engine.evaluate(
            FinalCandidateAction(
                raw=cast(JsonValue, thaw_json(frozen_output)),
                representation="run_graph",
            )
        )
        if not evaluation.accepted:
            raise GraphError(
                "Graph terminal output did not satisfy its output contract",
                issues=[
                    {
                        "path": list(issue.path),
                        "code": issue.code,
                        "message": issue.message,
                    }
                    for issue in evaluation.issues
                ],
            )
        message = AIMessage(
            content=json.dumps(
                thaw_json(frozen_output),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            cause_by=CauseBy.RUN_COMMAND,
        )
        return await engine.commit_final(message)

    async def resume(self, *, contract_spec: GraphOutputContractSpec, run_id: str) -> CommittedOutput[JsonValue] | None:
        """Finish a replayed accepted Graph output without rerunning its graph."""
        restored = self._take_restore(run_id)
        if restored is None:
            return None
        engine = self._engine(
            contract_spec=contract_spec,
            run_id=run_id,
            restored_state=restored,
        )
        if not engine.has_restored_terminal_output:
            return None
        return engine.committed_output


__all__ = ["GraphOutputService"]
