"""Media providers projected onto Runtime's lifecycle-neutral service port."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, cast
from urllib.parse import urlparse

import aiohttp

from mote.contracts.config.multimodal import MultimodalConfig
from mote.contracts.models.failover import (
    AttemptBudget,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    Retryability,
)
from mote.contracts.services import (
    ServiceAcceptance,
    ServiceAccepted,
    ServiceCompleted,
    ServiceEndpointDescriptor,
    ServiceEndpointFailure,
    ServiceEndpointOutcome,
    ServiceFailed,
    ServiceInvocation,
    ServiceReceipt,
    ServiceResponse,
)
from mote.product.errors.media import MediaGenerationError
from mote.product.toolsets.builtin.generate_media.registry import MediaProvider, MediaProviderRegistry
from mote.runtime.models.failover.admission import AdmissionRejectedError
from mote.runtime.models.failover.policy import classify_failure
from mote.runtime.service_gateway.snapshot import ServiceFailoverGroup, ServiceRuntimeSnapshot

_KINDS = ("image", "audio", "music", "video")


class MediaServiceEndpointAdapter:
    """Translate one configured media provider without owning its lifecycle."""

    def __init__(
        self,
        *,
        endpoint_id: str,
        credential_slot_id: str,
        tenant_fingerprint: str,
        provider: MediaProvider,
    ) -> None:
        self.endpoint_id = endpoint_id
        self.credential_slot_id = credential_slot_id
        self.tenant_fingerprint = tenant_fingerprint
        self._provider = provider

    async def start_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        item = _invocation_item(invocation)
        try:
            operation_id = await self._provider.start_once(
                item,
                idempotency_key=invocation.idempotency_key,
                timeout_seconds=timeout_seconds,
            )
        except MediaGenerationError as exc:
            return ServiceFailed(failure=_classify_media_exception(exc))
        return ServiceAccepted(
            receipt=ServiceReceipt(
                provider_operation_id=operation_id,
                state={
                    "filename": str(item.get("filename") or _default_filename(self._provider.kind)),
                    "item": item,
                },
                poll_after_seconds=3.0,
            )
        )

    async def poll_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome:
        state = cast(dict[str, Any], dict(receipt.state))
        try:
            completed = await self._provider.poll_once(
                receipt.provider_operation_id,
                state,
                timeout_seconds=timeout_seconds,
            )
        except MediaGenerationError as exc:
            return ServiceFailed(failure=_classify_media_exception(exc))
        if completed is None:
            return ServiceAccepted(receipt=receipt)
        return ServiceCompleted(
            response=ServiceResponse(
                value=completed,
                provider_request_id=receipt.provider_operation_id,
            )
        )

    async def reconcile_once(
        self,
        invocation: ServiceInvocation,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> ServiceEndpointOutcome | None:
        item = _invocation_item(invocation)
        reconciled = await self._provider.reconcile_once(
            item,
            idempotency_key=invocation.idempotency_key,
            timeout_seconds=timeout_seconds,
        )
        if reconciled is None:
            return None
        operation_id, completed = reconciled
        if completed is not None:
            return ServiceCompleted(
                response=ServiceResponse(
                    value=completed,
                    provider_request_id=operation_id,
                )
            )
        return ServiceAccepted(
            receipt=ServiceReceipt(
                provider_operation_id=operation_id,
                state={
                    "filename": str(item.get("filename") or _default_filename(self._provider.kind)),
                    "item": item,
                },
                poll_after_seconds=3.0,
            )
        )

    async def cancel_once(
        self,
        receipt: ServiceReceipt,
        endpoint: ServiceEndpointDescriptor,
        *,
        timeout_seconds: float,
    ) -> None:
        await self._provider.cancel_once(
            receipt.provider_operation_id,
            timeout_seconds=timeout_seconds,
        )

    def classify_start(self, exc: Exception) -> ServiceEndpointFailure:
        if isinstance(exc, AdmissionRejectedError):
            return ServiceEndpointFailure(
                disposition=exc.disposition,
                acceptance=ServiceAcceptance.REJECTED,
            )
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        rejected = isinstance(status, int) and 400 <= status < 500
        return ServiceEndpointFailure(
            disposition=_classify_media_exception(exc),
            acceptance=(ServiceAcceptance.REJECTED if rejected else ServiceAcceptance.UNKNOWN),
        )

    def classify_poll(self, exc: Exception) -> ServiceEndpointFailure:
        if isinstance(exc, AdmissionRejectedError):
            return ServiceEndpointFailure(
                disposition=exc.disposition,
                acceptance=ServiceAcceptance.REJECTED,
            )
        return ServiceEndpointFailure(
            disposition=_classify_media_exception(exc),
            acceptance=ServiceAcceptance.UNKNOWN,
        )

    async def aclose(self) -> None:
        return None


class MediaServiceEndpointResolver:
    """Resolve immutable endpoint identities into configured media adapters."""

    def __init__(
        self,
        multimodal: MultimodalConfig,
        providers: MediaProviderRegistry,
    ) -> None:
        self._multimodal = multimodal
        self._providers = providers

    def resolve(
        self,
        endpoint: ServiceEndpointDescriptor,
        credential_slot_id: str,
    ) -> MediaServiceEndpointAdapter | None:
        if not endpoint.capability.startswith("media.generate."):
            return None
        kind = _kind_for_capability(endpoint.capability)
        config = getattr(self._multimodal, f"{kind}_generation")
        expected_slot = _credential_slot_id(kind, str(config.api_key))
        if credential_slot_id != expected_slot:
            return None
        provider = self._providers.create(kind, config)
        return MediaServiceEndpointAdapter(
            endpoint_id=endpoint.endpoint_id,
            credential_slot_id=credential_slot_id,
            tenant_fingerprint=_fingerprint(str(config.api_key)),
            provider=provider,
        )

    async def aclose(self) -> None:
        return None


def build_media_service_snapshot(
    multimodal: MultimodalConfig,
) -> ServiceRuntimeSnapshot:
    endpoints: list[ServiceEndpointDescriptor] = []
    groups: list[ServiceFailoverGroup] = []
    route_groups: list[tuple[str, str]] = []
    credential_slots: list[tuple[str, tuple[str, ...]]] = []
    revisions: list[str] = []
    for kind in _KINDS:
        config = getattr(multimodal, f"{kind}_generation")
        if not config.base_url or not config.api_key:
            continue
        provider = str(config.provider or "openai")
        endpoint_id = f"media.{kind}.{provider}"
        capability = f"media.generate.{kind}"
        revision = _config_revision(kind, config)
        endpoint = ServiceEndpointDescriptor(
            endpoint_id=endpoint_id,
            capability=capability,
            transport=urlparse(str(config.base_url)).scheme or "https",
            provider=provider,
            base_url_identity=_fingerprint(str(config.base_url).rstrip("/")),
            credential_pool_id=f"{endpoint_id}.credentials",
            lifecycle_revision=revision,
        )
        group_id = f"media.{kind}"
        endpoints.append(endpoint)
        groups.append(
            ServiceFailoverGroup(
                group_id=group_id,
                endpoint_ids=(endpoint_id,),
                budget=AttemptBudget(
                    max_wire_attempts=4,
                    max_attempts_per_endpoint=4,
                    max_endpoint_switches=0,
                    max_credential_rotations=0,
                    max_request_transforms=0,
                    total_deadline_seconds=900.0,
                    single_attempt_timeout_seconds=300.0,
                    max_backoff_seconds=30.0,
                ),
            )
        )
        route_groups.append((group_id, group_id))
        credential_slots.append((endpoint_id, (_credential_slot_id(kind, str(config.api_key)),)))
        revisions.append(revision)
    return ServiceRuntimeSnapshot(
        revision=_fingerprint("\0".join(revisions) or "media-empty"),
        endpoints=tuple(endpoints),
        groups=tuple(groups),
        route_groups=tuple(route_groups),
        credential_slots=tuple(credential_slots),
    )


def _invocation_item(invocation: ServiceInvocation) -> dict[str, Any]:
    item = invocation.payload.get("item")
    if not isinstance(item, dict):
        raise ValueError("media service invocation requires an object payload.item")
    return cast(dict[str, Any], dict(item))


def _kind_for_capability(capability: str) -> str:
    prefix = "media.generate."
    if not capability.startswith(prefix):
        raise ValueError(f"unsupported media capability {capability!r}")
    kind = capability.removeprefix(prefix)
    if kind not in _KINDS:
        raise ValueError(f"unsupported media kind {kind!r}")
    return kind


def _credential_slot_id(kind: str, api_key: str) -> str:
    return f"media.{kind}.credential.{_fingerprint(api_key)}"


def _config_revision(kind: str, config: Any) -> str:
    models = (
        str(getattr(config, "model", "")),
        str(getattr(config, "text_to_video_model", "")),
        str(getattr(config, "reference_guided_video_model", "")),
    )
    return _fingerprint("\0".join((kind, str(config.provider), str(config.base_url), *models)))


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _default_filename(kind: str) -> str:
    return {
        "image": "image.png",
        "audio": "audio.mp3",
        "music": "music.wav",
        "video": "video.mp4",
    }[kind]


def _classify_media_exception(exc: Exception) -> FailureDisposition:
    if isinstance(exc, AdmissionRejectedError):
        return exc.disposition
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429 or "ratelimit" in name:
        return _failure(
            FailureReason.RATE_LIMITED,
            FailureDomain.TRANSPORT,
            Retryability.SAME_ENDPOINT,
            HealthVerdict.QUOTA_LIMITED,
            status,
        )
    if status in {401, 403} or "authentication" in name or "permission" in name:
        return _failure(
            FailureReason.AUTH_REJECTED,
            FailureDomain.CREDENTIAL,
            Retryability.AFTER_CHANGE,
            HealthVerdict.CREDENTIAL_REJECTED,
            status,
        )
    if status == 402:
        return _failure(
            FailureReason.BILLING_EXHAUSTED,
            FailureDomain.CREDENTIAL,
            Retryability.AFTER_CHANGE,
            HealthVerdict.CREDENTIAL_REJECTED,
            status,
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        return _failure(
            FailureReason.TIMEOUT,
            FailureDomain.TRANSPORT,
            Retryability.SAME_ENDPOINT,
            HealthVerdict.AVAILABILITY_FAILURE,
            status,
        )
    if isinstance(exc, (ConnectionError, aiohttp.ClientConnectionError)) or "connection" in name:
        return _failure(
            FailureReason.CONNECTION,
            FailureDomain.TRANSPORT,
            Retryability.SAME_ENDPOINT,
            HealthVerdict.AVAILABILITY_FAILURE,
            status,
        )
    if isinstance(status, int) and status >= 500:
        return _failure(
            FailureReason.SERVER_ERROR,
            FailureDomain.ENDPOINT,
            Retryability.SAME_ENDPOINT,
            HealthVerdict.AVAILABILITY_FAILURE,
            status,
        )
    if isinstance(exc, MediaGenerationError):
        return _failure(
            FailureReason.SERVER_ERROR,
            FailureDomain.ENDPOINT,
            Retryability.SAME_ENDPOINT if exc.retryable else Retryability.NEVER,
            HealthVerdict.AVAILABILITY_FAILURE if exc.retryable else HealthVerdict.NEUTRAL,
            status,
        )
    return classify_failure(exc)


def _failure(
    reason: FailureReason,
    domain: FailureDomain,
    retryability: Retryability,
    health_verdict: HealthVerdict,
    status: Any,
) -> FailureDisposition:
    return FailureDisposition(
        reason=reason,
        domain=domain,
        retryability=retryability,
        health_verdict=health_verdict,
        status_code=status if isinstance(status, int) else None,
    )


__all__ = ["MediaServiceEndpointResolver", "build_media_service_snapshot"]
