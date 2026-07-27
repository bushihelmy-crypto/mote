"""Provider-neutral semantic routing facade over RoutingService and ModelGateway."""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from mote.contracts.models.routing import RoutingDecision, RoutingInput, RoutingSignals
from mote.contracts.ports import ArtifactResolver, ContextReducer, ModelGateway, ModelRoute, SessionFactSink
from mote.runtime.errors import ModelNotFoundError
from mote.runtime.logging import log_class
from mote.runtime.models.failover.transforms import CanonicalRequestTransformer
from mote.runtime.models.routing.service import RoutingService

DEFAULT_MODEL_NAME = "default"
COMPRESSION_TASK = "compression"


@log_class(level="DEBUG")
class LLMRouter:
    """Bind guarded logical route decisions to the canonical ModelGateway."""

    def __init__(
        self,
        gateway: ModelGateway | None,
        *,
        routing_service: RoutingService | None = None,
        task_map: Optional[dict[str, str]] = None,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> None:
        self.gateway = gateway
        self.routing_service = routing_service
        self.routing_enabled = routing_service is not None
        self.task_map = dict(task_map or {})
        self._session_fact_sink = session_fact_sink
        self._artifact_resolver = artifact_resolver
        self.context_reducer: ContextReducer | None = None

    def map_task(self, task: str, route_id: str) -> None:
        self.task_map[task] = route_id

    def model_route_for_task(self, task: str) -> ModelRoute:
        return self.model_route(
            self.task_map.get(task, DEFAULT_MODEL_NAME),
            compression=task == COMPRESSION_TASK,
        )

    def model_route(
        self,
        route_id: str = DEFAULT_MODEL_NAME,
        *,
        compression: bool = False,
        routing_decision_id: str | None = None,
    ) -> ModelRoute:
        gateway = self.gateway
        if gateway is None:
            raise ModelNotFoundError(
                "Canonical ModelGateway is not installed in the Runtime context",
                requested=route_id,
            )
        if not gateway.supports_route(route_id):
            raise ModelNotFoundError(
                f"Canonical model route is unavailable: {route_id!r}",
                requested=route_id,
            )
        profile = gateway.route_profile(route_id)
        if profile is None:
            raise ModelNotFoundError(
                f"Canonical model route has no endpoint profile: {route_id!r}",
                requested=route_id,
            )
        return ModelRoute(
            gateway=gateway,
            route_id=route_id,
            profile=profile,
            routing_decision_id=routing_decision_id,
            request_transformer=(
                None
                if compression or self.context_reducer is None
                else CanonicalRequestTransformer(self.context_reducer)
            ),
            session_fact_sink=self._session_fact_sink,
            artifact_resolver=self._artifact_resolver,
        )

    async def aroute_model(
        self,
        routing_input: RoutingInput,
    ) -> tuple[ModelRoute, RoutingDecision]:
        if self.routing_service is None:
            raise RuntimeError("semantic routing is not configured for this agent")
        decision = await self.routing_service.decide(routing_input)
        route = self.model_route(
            decision.selected_route_id,
            routing_decision_id=decision.decision_id,
        )
        return route, decision

    async def seed_session(self, session_id: str, prompt: str) -> str | None:
        if self.routing_service is None:
            return None
        return await self.routing_service.seed_session(
            RoutingInput(
                decision_id=uuid4().hex,
                session_id=session_id,
                task="spawn_seed",
                signals=RoutingSignals(prompt_text=prompt),
            )
        )


__all__ = ["COMPRESSION_TASK", "DEFAULT_MODEL_NAME", "LLMRouter"]
