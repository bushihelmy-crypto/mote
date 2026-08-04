"""Exact-cache decorator around the single logical model gateway."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mote.contracts.events.inference import ModelCacheDegraded, ModelCacheHitRecord
from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.invocation import (
    CanonicalModelResponse,
    GenerateInput,
    ModelInvocation,
    ResolvedModelResponse,
    ResponseMode,
)
from mote.contracts.model.operations import ModelOperation
from mote.contracts.model.topology import RouteId
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.ports.inference.cache import InferenceCache
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.model.recovery import ModelRecoveryInspection
from mote.contracts.ports.model.request_transformer import ModelRequestTransformer
from mote.contracts.ports.session.facts import SessionFactSink
from mote.runtime.events.context import observe_event_sync
from mote.runtime.inference.cache import ExactCacheIdentity, exact_cache_key


class ExactCachedModelGateway:
    """Cache planning layer; the wrapped gateway remains the only wire owner."""

    def __init__(
        self,
        gateway: ModelGateway,
        cache: InferenceCache,
        *,
        identity: ExactCacheIdentity,
        ttl_seconds: int,
        sensitive_data_allowed: bool = False,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("exact cache TTL must be positive")
        self._gateway = gateway
        self._cache = cache
        self._identity = identity
        self._ttl_seconds = ttl_seconds
        self._sensitive_data_allowed = sensitive_data_allowed

    def supports_route(self, route_id: RouteId) -> bool:
        return self._gateway.supports_route(route_id)

    def route_profile(self, route_id: RouteId) -> EndpointDescriptor | None:
        return self._gateway.route_profile(route_id)

    def route_profiles(self, route_id: RouteId) -> tuple[EndpointDescriptor, ...]:
        return self._gateway.route_profiles(route_id)

    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection:
        return self._gateway.inspect_recovery(model_call_id)

    async def execute(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        if not self._cacheable(invocation, stream=stream):
            return await self._gateway.execute(
                invocation,
                request_transformer=request_transformer,
                stream=stream,
                session_fact_sink=session_fact_sink,
                artifact_resolver=artifact_resolver,
            )
        key = exact_cache_key(self._identity, invocation)
        now = datetime.now(timezone.utc)
        try:
            cached = await self._cache.get(key, now=now)
        except Exception as exc:
            observe_event_sync(ModelCacheDegraded(operation="get", cache_kind="exact", error_code=type(exc).__name__))
            cached = None
        if cached is not None:
            observe_event_sync(
                ModelCacheHitRecord(
                    model_call_id=invocation.model_call_id,
                    cache_kind="exact",
                    tenant_id=self._identity.tenant_id,
                    namespace=self._identity.namespace,
                )
            )
            return self._resolved_cache_hit(invocation, cached)
        response = await self._gateway.execute(
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
        )
        canonical = CanonicalModelResponse(
            output=response.output,
            usage=response.usage,
            cost_usd=response.cost_usd,
        )
        try:
            await self._cache.put(
                key,
                canonical,
                tenant_id=self._identity.tenant_id,
                namespace=self._identity.namespace,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
        except Exception as exc:
            observe_event_sync(ModelCacheDegraded(operation="put", cache_kind="exact", error_code=type(exc).__name__))
        return response

    async def resume(
        self,
        invocation: ModelInvocation,
        *,
        request_transformer: ModelRequestTransformer | None = None,
        stream: bool = False,
        session_fact_sink: SessionFactSink | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> ResolvedModelResponse:
        return await self._gateway.resume(
            invocation,
            request_transformer=request_transformer,
            stream=stream,
            session_fact_sink=session_fact_sink,
            artifact_resolver=artifact_resolver,
        )

    def _cacheable(self, invocation: ModelInvocation, *, stream: bool) -> bool:
        if stream or invocation.operation is not ModelOperation.GENERATE:
            return False
        model_input = invocation.input
        if not isinstance(model_input, GenerateInput):
            return False
        requirements = invocation.requirements
        if (
            model_input.tools
            or model_input.output_schema is not None
            or requirements.needs_tools
            or requirements.needs_native_schema
            or requirements.needs_server_web_search
            or requirements.needs_vision
            or requirements.needs_pdf
            or requirements.needs_native_tool_search
            or requirements.response_mode is not ResponseMode.TEXT
        ):
            return False
        classification = requirements.data_classification.strip().lower()
        return self._sensitive_data_allowed or classification in {"", "default", "public"}

    @staticmethod
    def _resolved_cache_hit(invocation: ModelInvocation, response: CanonicalModelResponse) -> ResolvedModelResponse:
        return ResolvedModelResponse(
            output=response.output,
            usage=response.usage,
            cost_usd=response.cost_usd,
            endpoint_id="cache:exact",
            endpoint_fingerprint="cache:exact",
            model_or_deployment="cache:exact",
            tenant_fingerprint="cache:tenant-isolated",
            credential_slot_id="cache:none",
            provider="cache",
            transport="none",
            model_call_id=invocation.model_call_id,
            successful_attempt_id=None,
        )


__all__ = ["ExactCachedModelGateway"]
