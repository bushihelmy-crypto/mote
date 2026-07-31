"""Compatibility owners backed by durable-command and transfer gateways."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Protocol, cast

from pydantic import JsonValue

from mote.contracts.artifact import (
    ArtifactPublishRequest,
    ArtifactRef,
    ArtifactRepresentationInput,
    ArtifactRetention,
    ArtifactSensitivity,
)
from mote.contracts.ports.artifact.store import ArtifactLookupIndex
from mote.product.interfaces.inference_api.operations import ArtifactCompatibilityOwner, DurableCompatibilityOwner


class CommandGateway(Protocol):
    async def execute(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        ...


class TransferGateway(Protocol):
    async def execute_part(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        ...


class CommandCompatibilityOwner(DurableCompatibilityOwner):
    def __init__(self, gateway: CommandGateway) -> None:
        self._gateway = gateway

    async def execute(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        wire_operation = _durable_operation(operation, mutation=True)
        return cast(dict[str, JsonValue], await self._gateway.execute(wire_operation, payload))

    async def list(self, operation: str, query: dict[str, str]) -> dict[str, JsonValue]:
        wire_operation = _durable_operation(operation, mutation=False)
        return cast(dict[str, JsonValue], await self._gateway.execute(wire_operation, query))

    async def resource(self, operation: str, resource_id: str) -> dict[str, JsonValue]:
        wire_operation, key = _resource_operation(operation)
        return cast(
            dict[str, JsonValue],
            await self._gateway.execute(wire_operation, {key: resource_id}),
        )

    async def content(self, resource_id: str) -> ArtifactRef:
        result = await self._gateway.execute("file.content", {"file_id": resource_id})
        value = result.get("artifact")
        if not isinstance(value, dict):
            raise RuntimeError("file content result omitted artifact reference")
        try:
            return ArtifactRef(**value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("file content result has invalid artifact reference") from exc


class ResponseCompatibilityOwner:
    def __init__(self, gateway: CommandGateway) -> None:
        self._gateway = gateway

    async def retrieve(self, response_id: str) -> dict[str, JsonValue] | None:
        return cast(
            dict[str, JsonValue], await self._gateway.execute("response.retrieve", {"response_id": response_id})
        )

    async def cancel(self, response_id: str) -> dict[str, JsonValue] | None:
        return cast(dict[str, JsonValue], await self._gateway.execute("response.cancel", {"response_id": response_id}))

    async def delete(self, response_id: str) -> dict[str, JsonValue] | None:
        return cast(dict[str, JsonValue], await self._gateway.execute("response.delete", {"response_id": response_id}))

    async def input_items(self, response_id: str, query: dict[str, str]) -> dict[str, JsonValue] | None:
        return cast(
            dict[str, JsonValue],
            await self._gateway.execute("response.input_items", {"response_id": response_id, **query}),
        )


class ArtifactTransferCompatibilityOwner(ArtifactCompatibilityOwner):
    def __init__(self, gateway: TransferGateway, artifact_store: ArtifactLookupIndex | None = None) -> None:
        self._gateway = gateway
        self._artifact_store = artifact_store

    async def upload(self, operation: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if operation != "files.upload":
            raise ValueError(f"unsupported artifact operation {operation!r}")
        return cast(
            dict[str, JsonValue],
            await self._gateway.execute_part("file.upload", payload),
        )

    async def upload_bytes(
        self,
        operation: str,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        fields: dict[str, str],
    ) -> dict[str, JsonValue]:
        if operation != "files.upload":
            raise ValueError(f"unsupported artifact operation {operation!r}")
        if self._artifact_store is None:
            raise RuntimeError("file upload artifact store is unavailable")
        purpose = fields.get("purpose", "assistants")
        revision = await self._artifact_store.publish(
            ArtifactPublishRequest(
                idempotency_key=fields.get("idempotency_key", ""),
                retention=ArtifactRetention.PROJECT,
                sensitivity=ArtifactSensitivity.PRIVATE,
                representations=(
                    ArtifactRepresentationInput(
                        representation="original",
                        kind="provider_file",
                        mime_type=content_type,
                        content=content,
                        suggested_name=filename,
                    ),
                ),
            )
        )
        artifact = revision.get("original")
        payload: dict[str, object] = {
            "artifact": asdict(artifact),
            "purpose": purpose,
        }
        return cast(
            dict[str, JsonValue],
            await self._gateway.execute_part("file.upload", payload),
        )


def _durable_operation(operation: str, *, mutation: bool) -> str:
    if operation == "batches":
        return "batch.create" if mutation else "batch.list"
    if operation == "files" and not mutation:
        return "file.list"
    if operation == "videos":
        return "video.generate" if mutation else "video.list"
    if operation == "containers":
        return "container.create" if mutation else "container.list"
    raise ValueError(f"unsupported durable compatibility operation {operation!r}")


def _resource_operation(operation: str) -> tuple[str, str]:
    if operation.startswith("batch."):
        if operation not in {"batch.retrieve", "batch.cancel", "batch.delete"}:
            raise ValueError(f"unsupported batch operation {operation!r}")
        return operation, "batch_id"
    if operation.startswith("file."):
        if operation not in {"file.retrieve", "file.delete", "file.content"}:
            raise ValueError(f"unsupported file operation {operation!r}")
        return operation, "file_id"
    if operation.startswith("video."):
        if operation not in {
            "video.retrieve",
            "video.delete",
            "video.download",
            "video.remix",
        }:
            raise ValueError(f"unsupported video operation {operation!r}")
        return operation, "video_id"
    if operation.startswith("container."):
        if operation not in {"container.retrieve", "container.delete"}:
            raise ValueError(f"unsupported container operation {operation!r}")
        return operation, "container_id"
    raise ValueError(f"unsupported resource operation {operation!r}")


__all__ = [
    "ArtifactTransferCompatibilityOwner",
    "CommandCompatibilityOwner",
    "ResponseCompatibilityOwner",
]
