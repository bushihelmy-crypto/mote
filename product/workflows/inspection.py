"""Product-owned immutable workflow run inspection registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mote.orchestration.workflows import RunSnapshot, WorkflowRun


@dataclass
class WorkflowTaskView:
    task_id: str
    run: WorkflowRun = field(repr=False)
    graph_meta: Any = field(default=None, repr=False)
    max_restarts: int = 3
    retry_count: int = 0
    task_meta: Any = field(default=None, repr=False)

    @property
    def run_state(self):
        return self.graph_meta.run_state if self.graph_meta is not None else self.run._run_state

    @property
    def state_snapshot(self):
        return self.run._state

    @state_snapshot.setter
    def state_snapshot(self, value: Any) -> None:
        if self.graph_meta is not None:
            self.graph_meta.state = value
        self.run._state = value

    @property
    def completed_nodes(self) -> set[str]:
        run_state = self.run_state
        return set(run_state.completed_names()) if run_state is not None else set()

    def __getattr__(self, name: str):
        if self.task_meta is None:
            raise AttributeError(name)
        return getattr(self.task_meta, name)


class WorkflowInspectionPort:
    def __init__(self) -> None:
        self._views: dict[str, WorkflowTaskView] = {}

    def register(
        self,
        task_id: str,
        run: WorkflowRun,
        graph_meta: Any = None,
        *,
        max_restarts: int = 3,
    ) -> str:
        previous = self._views.get(task_id)
        self._views[task_id] = WorkflowTaskView(
            task_id=task_id,
            run=run,
            graph_meta=(previous.graph_meta if graph_meta is None and previous is not None else graph_meta),
            max_restarts=previous.max_restarts if previous is not None else max_restarts,
            retry_count=(previous.retry_count + 1) if previous is not None else 0,
        )
        return task_id

    def snapshot(self, task_id: str) -> RunSnapshot | None:
        view = self._views.get(task_id)
        return view.run.snapshot() if view is not None else None

    def view(self, task_id: str, task_meta: Any = None) -> WorkflowTaskView | None:
        view = self._views.get(task_id)
        if view is not None and task_meta is not None:
            view.task_meta = task_meta
        return view

    def record_retry(self, task_id: str) -> None:
        view = self._views.get(task_id)
        if view is not None:
            view.retry_count += 1

    def discard(self, task_id: str) -> None:
        self._views.pop(task_id, None)

    async def aclose(self) -> None:
        self._views.clear()


__all__ = ["WorkflowInspectionPort", "WorkflowTaskView"]
