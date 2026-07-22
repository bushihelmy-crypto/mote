"""Graph-specific terminal output validation, commit, and recovery."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from mote.common.exception import GraphError
from mote.common.interface import CommitFence
from mote.common.schema import FinalCandidateAction, RunKind
from mote.roles.output_contract import OutputContract
from mote.roles.output_engine import OutputEngine


class GraphOutputLease(CommitFence, Protocol):
    @property
    def fencing_token(self) -> int:
        ...


class GraphOutputService:
    """Own Graph terminal-output semantics without pretending Graph is an Agent run."""

    def __init__(
        self,
        *,
        take_restore: Callable[[str], dict | None],
        current_lease: Callable[[str], GraphOutputLease],
    ) -> None:
        self._take_restore = take_restore
        self._current_lease = current_lease

    @staticmethod
    def _contract(contract_spec: Any) -> OutputContract[Any]:
        return OutputContract.from_json_schema(
            contract_spec.schema_,
            namespace=contract_spec.namespace,
            name=contract_spec.name,
            version=contract_spec.version,
        )

    def _engine(
        self,
        *,
        contract_spec: Any,
        run_id: str,
        restored_state: dict | None = None,
    ) -> OutputEngine:
        lease = self._current_lease(run_id)
        return OutputEngine(
            self._contract(contract_spec),
            restored_state=restored_state,
            run_id=run_id,
            run_kind=RunKind.GRAPH,
            commit_fence=lease,
            fencing_token=lease.fencing_token,
        )

    async def finalize(self, *, output: Any, contract_spec: Any, run_id: str) -> Any:
        """Validate and commit one model-authored Graph terminal value."""
        engine = self._engine(contract_spec=contract_spec, run_id=run_id)
        evaluation = await engine.evaluate(FinalCandidateAction(raw=output, representation="run_graph"))
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
        return await engine.commit()

    async def resume(self, *, contract_spec: Any, run_id: str) -> Any:
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
        return await engine.commit()


__all__ = ["GraphOutputService"]
