"""Runtime implementation of the two-stage model inference port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from uuid import uuid4

from mote.contracts.events.application import InferenceTargetCapacityReached, InferenceTargetExpired
from mote.contracts.model.inference import (
    EndpointCapabilitySnapshot,
    FinalizedInferenceRequest,
    InferenceAttemptFence,
    InferenceIntent,
    InferenceResult,
    InferenceTargetLease,
    ResolvedInferenceTarget,
    TargetInvalidated,
)
from mote.contracts.model.invocation import RequestRequirements, ResponseMode, TraceContext
from mote.contracts.model.routing import RoutingHints, RoutingInput, RoutingMessage, RoutingSignals
from mote.contracts.model.topology_codec import encode_route_id
from mote.contracts.ports.model.gateway import ModelRoute
from mote.kernel.telemetry.context import current_trace_id
from mote.runtime.events import observe_event_sync
from mote.runtime.models.model_calls import generate


class TargetCapacityError(RuntimeError):
    pass


@dataclass(slots=True)
class _PinnedTarget:
    route: ModelRoute
    runtime_lease: object | None = None
    expires_at: float = 0.0
    active_calls: int = 0
    release_requested: bool = False
    released: bool = False
    completion: asyncio.Future[None] | None = field(init=False, default=None)

    def completion_future(self) -> asyncio.Future[None]:
        if self.completion is None:
            self.completion = asyncio.get_running_loop().create_future()
        return self.completion


class RuntimeModelInferencePort:
    _TARGET_CAPACITY = 1024
    _TARGET_TTL_SECONDS = 600.0

    def __init__(self, *, router=None, role=None) -> None:
        self._router = router
        self._role = role
        self._targets: dict[str, _PinnedTarget] = {}
        self._target_lock = asyncio.Lock()
        self._attempts: dict[str, tuple[int, str]] = {}
        self._results: dict[tuple[str, str], InferenceResult] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future[InferenceResult | TargetInvalidated]] = {}

    async def resolve(self, intent: InferenceIntent) -> ResolvedInferenceTarget:
        role = self._role
        if role is None:
            raise RuntimeError("model inference port has no Runtime role")
        await self._reap_expired()
        async with self._target_lock:
            if len(self._targets) >= self._TARGET_CAPACITY:
                observe_event_sync(
                    InferenceTargetCapacityReached(target_count=len(self._targets), limit=self._TARGET_CAPACITY)
                )
                raise TargetCapacityError("inference target registry is full")
        if self._router is None:
            raise RuntimeError("model inference port has no Runtime router")
        if self._router.routing_enabled and intent.routing_messages:
            requirements = intent.requirements
            response_mode = (
                ResponseMode.NATIVE_SCHEMA
                if requirements.native_schema
                else (ResponseMode.NATIVE_TOOLS if requirements.tool_calling else ResponseMode.TEXT)
            )
            signals = RoutingSignals(
                messages=tuple(RoutingMessage(role=role, content=content) for role, content in intent.routing_messages),
                estimated_tokens=intent.estimated_tokens,
                conversation_turns=sum(1 for role, _content in intent.routing_messages if role == "user"),
            )
            route, _decision = await self._router.aroute_model(
                RoutingInput(
                    decision_id=uuid4().hex,
                    model_call_id=intent.model_call_id,
                    session_id=role.session_id,
                    turn_id=role.state.turn_index,
                    task="interactive",
                    requirements=RequestRequirements(
                        response_mode=response_mode,
                        needs_tools=requirements.tool_calling,
                        needs_native_schema=requirements.native_schema,
                        needs_vision="image" in requirements.multimodal,
                        needs_pdf="pdf" in requirements.multimodal,
                        needs_native_tool_search=requirements.native_tool_search,
                        min_context_tokens=intent.estimated_tokens,
                    ),
                    signals=signals,
                    caller_hints=RoutingHints(),
                    trace=TraceContext(trace_id=current_trace_id() or ""),
                )
            )
        else:
            route = self._router.model_route()
        runtime_lease = await role._components.acquire_runtime_composition()
        try:
            profile = runtime_lease.gateway.route_profile(route.route_id)
            if profile is None:
                raise RuntimeError("selected route is absent from the pinned generation")
            pinned_route = ModelRoute(
                gateway=runtime_lease.gateway,
                route_id=route.route_id,
                profile=profile,
                routing_decision_id=route.routing_decision_id,
                request_transformer=route.request_transformer,
                session_fact_sink=route.session_fact_sink,
                artifact_resolver=route.artifact_resolver,
            )
            async with self._target_lock:
                if len(self._targets) >= self._TARGET_CAPACITY:
                    observe_event_sync(
                        InferenceTargetCapacityReached(target_count=len(self._targets), limit=self._TARGET_CAPACITY)
                    )
                    raise TargetCapacityError("inference target registry is full")
                return self.pin_route(pinned_route, runtime_lease=runtime_lease)
        except BaseException:
            await runtime_lease.aclose()
            raise

    def pin_route(self, route: ModelRoute, *, runtime_lease: object | None = None) -> ResolvedInferenceTarget:
        if len(self._targets) >= self._TARGET_CAPACITY:
            observe_event_sync(
                InferenceTargetCapacityReached(target_count=len(self._targets), limit=self._TARGET_CAPACITY)
            )
            raise TargetCapacityError("inference target registry is full")
        profile = route.profile
        capability_payload = profile.model_dump(mode="json")
        capability_fingerprint = hashlib.sha256(
            json.dumps(capability_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lease_id = uuid4().hex
        expires_at = time.time() + self._TARGET_TTL_SECONDS
        self._targets[lease_id] = _PinnedTarget(route, runtime_lease, expires_at)
        snapshot = EndpointCapabilitySnapshot(
            structured_output_modes=("native_schema",) if profile.capabilities.supports_native_schema else (),
            multimodal_envelope=tuple(
                kind
                for kind, enabled in (
                    ("image", profile.capabilities.supports_vision),
                    ("pdf", profile.capabilities.supports_pdf),
                )
                if enabled
            ),
            supports_resume=True,
            supports_native_tool_search=profile.capabilities.supports_native_tool_search,
            canonicalization_version=profile.lifecycle_revision,
        )
        compatibility = hashlib.sha256(f"{profile.transport}:{capability_fingerprint}".encode()).hexdigest()
        return ResolvedInferenceTarget(
            route_id=route.route_id,
            command_protocol="native" if profile.capabilities.supports_tools else "xml",
            command_protocol_version="1",
            capabilities=snapshot,
            capability_fingerprint=capability_fingerprint,
            projection_compatibility_key=compatibility,
            lease=InferenceTargetLease(encode_route_id(route.route_id), lease_id, expires_at),
        )

    def profile(self, target: ResolvedInferenceTarget):
        return self._route(target).profile

    async def infer(
        self,
        target: ResolvedInferenceTarget,
        request: FinalizedInferenceRequest,
        attempt: InferenceAttemptFence,
    ) -> InferenceResult | TargetInvalidated:
        if target.lease.expires_at <= time.time():
            await self.release(target)
            return TargetInvalidated("target lease expired", target.lease.target_id)
        if request.model_call_id != attempt.model_call_id:
            return TargetInvalidated("inference attempt identity mismatch", target.lease.target_id)
        pinned = await self._begin_call(target)
        attempt_key = (attempt.model_call_id, attempt.attempt_id)
        try:
            return await self._infer_pinned(target, request, attempt, attempt_key, pinned)
        finally:
            await self._end_call(target, pinned)

    async def _infer_pinned(
        self,
        target: ResolvedInferenceTarget,
        request: FinalizedInferenceRequest,
        attempt: InferenceAttemptFence,
        attempt_key: tuple[str, str],
        pinned: _PinnedTarget,
    ) -> InferenceResult | TargetInvalidated:
        cached = self._results.get(attempt_key)
        if cached is not None:
            return cached
        inflight = self._inflight.get(attempt_key)
        if inflight is not None:
            return await asyncio.shield(inflight)
        current = self._attempts.get(attempt.model_call_id)
        if current is not None and (
            attempt.fencing_token < current[0]
            or (attempt.fencing_token == current[0] and attempt.attempt_id != current[1])
        ):
            return TargetInvalidated("inference attempt was fenced", target.lease.target_id)
        self._attempts[attempt.model_call_id] = (
            attempt.fencing_token,
            attempt.attempt_id,
        )
        future = asyncio.get_running_loop().create_future()
        self._inflight[attempt_key] = future
        try:
            route = pinned.route
            payload = request.payload
            output, _resolved = await generate(route, **payload)
            if self._attempts.get(attempt.model_call_id) != (
                attempt.fencing_token,
                attempt.attempt_id,
            ):
                result: InferenceResult | TargetInvalidated = TargetInvalidated(
                    "inference attempt was fenced", target.lease.target_id
                )
            else:
                result = InferenceResult(
                    content=output.content or "",
                    tool_calls=[
                        {
                            "id": call.id,
                            "command_name": call.name,
                            "args": call.arguments,
                        }
                        for call in output.tool_calls
                    ]
                    if payload.get("tools") is not None
                    else None,
                    structured_value=getattr(output, "structured", None),
                )
                self._results[attempt_key] = result
            future.set_result(result)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()
            raise
        finally:
            self._inflight.pop(attempt_key, None)

    async def release(self, target: ResolvedInferenceTarget) -> None:
        detached = False
        runtime_lease = None
        async with self._target_lock:
            pinned = self._targets.get(target.lease.lease_id)
            if pinned is None:
                return
            pinned.release_requested = True
            if pinned.active_calls == 0:
                detached, runtime_lease = self._detach(target, pinned)
            completion = pinned.completion_future()
        if detached:
            await self._close_target(pinned, runtime_lease)
        await asyncio.shield(completion)

    async def aclose(self) -> None:
        completions: list[asyncio.Future[None]] = []
        async with self._target_lock:
            targets = tuple(self._targets.items())
        for lease_id, pinned in targets:
            detached = False
            runtime_lease = None
            async with self._target_lock:
                if self._targets.get(lease_id) is not pinned:
                    continue
                pinned.release_requested = True
                completions.append(pinned.completion_future())
                if pinned.active_calls == 0:
                    self._targets.pop(lease_id, None)
                    pinned.released = True
                    detached = True
                    runtime_lease = pinned.runtime_lease
            if detached:
                await self._close_target(pinned, runtime_lease)
        if completions:
            await asyncio.gather(*(asyncio.shield(completion) for completion in completions))

    async def _reap_expired(self) -> None:
        now = time.time()
        expired: list[tuple[_PinnedTarget, object | None]] = []
        async with self._target_lock:
            for lease_id, pinned in tuple(self._targets.items()):
                if pinned.active_calls == 0 and pinned.expires_at <= now:
                    self._targets.pop(lease_id, None)
                    pinned.released = True
                    expired.append((pinned, pinned.runtime_lease))
        for pinned, runtime_lease in expired:
            await self._close_target(pinned, runtime_lease)
            observe_event_sync(InferenceTargetExpired(target_state="ready", age_bucket="ttl"))

    def diagnostics(self) -> dict[str, object]:
        targets = tuple(self._targets.items())
        now = time.time()
        oldest = min(targets, key=lambda item: item[1].expires_at, default=None)
        return {
            "ready": sum(item.active_calls == 0 for _key, item in targets),
            "active": sum(item.active_calls > 0 for _key, item in targets),
            "oldest_target_id": oldest[0] if oldest is not None else None,
            "oldest_age_seconds": (
                max(
                    0.0,
                    now - (oldest[1].expires_at - self._TARGET_TTL_SECONDS),
                )
                if oldest is not None
                else 0.0
            ),
        }

    async def _begin_call(self, target: ResolvedInferenceTarget) -> _PinnedTarget:
        async with self._target_lock:
            pinned = self._targets.get(target.lease.lease_id)
            if (
                pinned is None
                or pinned.released
                or pinned.release_requested
                or pinned.route.route_id != target.route_id
            ):
                raise RuntimeError("resolved inference target is no longer pinned")
            pinned.active_calls += 1
            return pinned

    async def _end_call(self, target: ResolvedInferenceTarget, pinned: _PinnedTarget) -> None:
        detached = False
        runtime_lease = None
        async with self._target_lock:
            pinned.active_calls -= 1
            if pinned.active_calls == 0 and pinned.release_requested:
                detached, runtime_lease = self._detach(target, pinned)
        if detached:
            await self._close_target(pinned, runtime_lease)

    def _detach(self, target: ResolvedInferenceTarget, pinned: _PinnedTarget):
        if self._targets.get(target.lease.lease_id) is not pinned:
            return False, None
        self._targets.pop(target.lease.lease_id, None)
        pinned.released = True
        return True, pinned.runtime_lease

    @staticmethod
    async def _close_target(pinned: _PinnedTarget, runtime_lease) -> None:
        try:
            if runtime_lease is not None:
                await runtime_lease.aclose()
        except BaseException as exc:
            completion = pinned.completion_future()
            if not completion.done():
                completion.set_exception(exc)
            raise
        else:
            completion = pinned.completion_future()
            if not completion.done():
                completion.set_result(None)

    def _route(self, target: ResolvedInferenceTarget) -> ModelRoute:
        pinned = self._targets.get(target.lease.lease_id)
        if pinned is None or pinned.route.route_id != target.route_id:
            raise RuntimeError("resolved inference target is no longer pinned")
        return pinned.route


__all__ = ["RuntimeModelInferencePort", "TargetCapacityError"]
