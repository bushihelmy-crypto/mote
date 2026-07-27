"""Readiness and degradation model for the process-scoped Event Fabric."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from mote.contracts.events import StreamId
from mote.contracts.ports.event_subscription import Reliability, SubscriptionIdentity
from mote.runtime.events.dispatcher import CommittedEventDispatcher, DispatcherState
from mote.runtime.events.mailbox import MailboxSnapshot
from mote.runtime.events.subscription import SubscriptionState
from mote.runtime.events.telemetry import TelemetryRuntime, TelemetryState, TelemetrySubscriptionSnapshot


class FabricState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"
    CLOSED = "closed"


class FabricHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    READ_ONLY = "read_only"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FabricHealthIssue:
    component: str
    state: FabricHealthState
    detail: str
    since: datetime


@dataclass(frozen=True)
class StreamLag:
    stream_id: StreamId
    dispatched: int
    acknowledged: int
    persisted: int

    @property
    def delivery_lag(self) -> int:
        return max(0, self.dispatched - self.acknowledged)

    @property
    def durable_lag(self) -> int:
        return max(0, self.dispatched - self.persisted)


@dataclass(frozen=True)
class SubscriptionHealthSnapshot:
    identity: SubscriptionIdentity
    reliability: Reliability
    state: SubscriptionState
    mailbox: MailboxSnapshot
    failure: str | None
    streams: tuple[StreamLag, ...]


@dataclass(frozen=True)
class FabricHealthSnapshot:
    state: FabricHealthState
    ready: bool
    writable: bool
    issues: tuple[FabricHealthIssue, ...]
    subscriptions: tuple[SubscriptionHealthSnapshot, ...]
    telemetry: tuple[TelemetrySubscriptionSnapshot, ...]


_HEALTH_RANK = {
    FabricHealthState.HEALTHY: 0,
    FabricHealthState.DEGRADED: 1,
    FabricHealthState.READ_ONLY: 2,
    FabricHealthState.UNAVAILABLE: 3,
}


class FabricHealth:
    """Owned health registry combined with live dispatcher state on every read."""

    def __init__(self) -> None:
        self._issues: dict[str, FabricHealthIssue] = {}

    def mark_degraded(self, component: str, detail: str) -> None:
        self._mark(component, FabricHealthState.DEGRADED, detail)

    def mark_read_only(self, component: str, detail: str) -> None:
        self._mark(component, FabricHealthState.READ_ONLY, detail)

    def mark_unavailable(self, component: str, detail: str) -> None:
        self._mark(component, FabricHealthState.UNAVAILABLE, detail)

    def clear(self, component: str) -> None:
        self._issues.pop(component, None)

    def snapshot(
        self,
        fabric_state: FabricState,
        dispatcher: CommittedEventDispatcher,
        telemetry: TelemetryRuntime | None = None,
    ) -> FabricHealthSnapshot:
        issues = dict(self._issues)
        if fabric_state is FabricState.FAILED:
            self._derived_issue(
                issues,
                "fabric.lifecycle",
                FabricHealthState.UNAVAILABLE,
                "event fabric lifecycle failed",
            )
        if dispatcher.state is DispatcherState.FAILED:
            self._derived_issue(
                issues,
                "fabric.dispatcher",
                FabricHealthState.UNAVAILABLE,
                _failure_detail(dispatcher.failure, "committed dispatcher failed"),
            )

        subscriptions: list[SubscriptionHealthSnapshot] = []
        for worker in dispatcher.subscriptions:
            worker_snapshot = worker.snapshot()
            if worker_snapshot.state is SubscriptionState.FAILED:
                severity = (
                    FabricHealthState.UNAVAILABLE
                    if worker_snapshot.reliability is Reliability.DURABLE
                    else FabricHealthState.DEGRADED
                )
                self._derived_issue(
                    issues,
                    f"subscription.{worker_snapshot.identity}",
                    severity,
                    worker_snapshot.failure or "subscription worker failed",
                )
            elif worker_snapshot.state is SubscriptionState.DEGRADED:
                self._derived_issue(
                    issues,
                    f"subscription.{worker_snapshot.identity}",
                    FabricHealthState.DEGRADED,
                    worker_snapshot.failure or "subscription delivery degraded",
                )
            acknowledged = dict(worker_snapshot.acknowledged)
            persisted = dict(worker_snapshot.persisted)
            stream_lag: list[StreamLag] = []
            for stream_id in dispatcher.streams:
                if not worker.spec.event_filter.matches_stream(stream_id):
                    continue
                dispatched = dispatcher.cursor(stream_id)
                stream_lag.append(
                    StreamLag(
                        stream_id=stream_id,
                        dispatched=dispatched,
                        acknowledged=acknowledged.get(stream_id, 0),
                        persisted=persisted.get(stream_id, 0),
                    )
                )
            subscriptions.append(
                SubscriptionHealthSnapshot(
                    identity=worker_snapshot.identity,
                    reliability=worker_snapshot.reliability,
                    state=worker_snapshot.state,
                    mailbox=worker_snapshot.mailbox,
                    failure=worker_snapshot.failure,
                    streams=tuple(stream_lag),
                )
            )

        telemetry_snapshots = telemetry.snapshots() if telemetry is not None else ()
        if (
            telemetry is not None
            and fabric_state is FabricState.RUNNING
            and telemetry.state not in {TelemetryState.RUNNING, TelemetryState.DEGRADED}
        ):
            self._derived_issue(
                issues,
                "telemetry.lifecycle",
                FabricHealthState.DEGRADED,
                f"telemetry lifecycle is {telemetry.state.value}",
            )
        for telemetry_snapshot in telemetry_snapshots:
            mailbox = telemetry_snapshot.mailbox
            if telemetry_snapshot.state is TelemetryState.DEGRADED:
                self._derived_issue(
                    issues,
                    f"telemetry.{telemetry_snapshot.identity}",
                    FabricHealthState.DEGRADED,
                    telemetry_snapshot.last_failure or "telemetry handler degraded",
                )
            if mailbox.dropped or mailbox.coalesced:
                self._derived_issue(
                    issues,
                    f"telemetry.{telemetry_snapshot.identity}.mailbox",
                    FabricHealthState.DEGRADED,
                    f"dropped={mailbox.dropped} coalesced={mailbox.coalesced}",
                )

        ordered_issues = tuple(sorted(issues.values(), key=lambda item: item.component))
        state = max(
            (issue.state for issue in ordered_issues),
            key=lambda item: _HEALTH_RANK[item],
            default=FabricHealthState.HEALTHY,
        )
        running = fabric_state is FabricState.RUNNING
        return FabricHealthSnapshot(
            state=state,
            ready=running and state is not FabricHealthState.UNAVAILABLE,
            writable=running and state in {FabricHealthState.HEALTHY, FabricHealthState.DEGRADED},
            issues=ordered_issues,
            subscriptions=tuple(subscriptions),
            telemetry=telemetry_snapshots,
        )

    def _mark(
        self,
        component: str,
        state: FabricHealthState,
        detail: str,
    ) -> None:
        if type(component) is not str or not component:
            raise ValueError("health component must be non-empty")
        if type(detail) is not str or not detail:
            raise ValueError("health detail must be non-empty")
        existing = self._issues.get(component)
        since = existing.since if existing is not None and existing.state is state else datetime.now(timezone.utc)
        self._issues[component] = FabricHealthIssue(
            component=component,
            state=state,
            detail=detail,
            since=since,
        )

    @staticmethod
    def _derived_issue(
        issues: dict[str, FabricHealthIssue],
        component: str,
        state: FabricHealthState,
        detail: str,
    ) -> None:
        existing = issues.get(component)
        issues[component] = FabricHealthIssue(
            component=component,
            state=state,
            detail=detail,
            since=existing.since if existing is not None else datetime.now(timezone.utc),
        )


def _failure_detail(failure: BaseException | None, fallback: str) -> str:
    if failure is None:
        return fallback
    return f"{type(failure).__name__}: {failure}"


__all__ = [
    "FabricHealth",
    "FabricHealthIssue",
    "FabricHealthSnapshot",
    "FabricHealthState",
    "FabricState",
    "StreamLag",
    "SubscriptionHealthSnapshot",
]
