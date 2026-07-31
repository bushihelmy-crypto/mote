"""Ownership-safe Product model generation build-or-reuse entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mote.product.config.model.inputs import ProductModelsConfig
from mote.product.models.compiler import (
    AdapterFactoryRevision,
    CompiledModelGeneration,
    CredentialSourceCatalog,
    ModelGenerationReuseKey,
    ProviderCatalogRevision,
    compile_model_generation,
    prepare_model_generation,
)
from mote.product.models.secrets import SecretHandle


@dataclass(frozen=True, slots=True)
class ProductModelGenerationReuseKey:
    model: ModelGenerationReuseKey
    inference_revision: str

    def __post_init__(self) -> None:
        if not self.inference_revision:
            raise ValueError("inference reuse revision is required")


class ReusableModelGeneration(Protocol):
    @property
    def reuse_key(self) -> ProductModelGenerationReuseKey:
        ...

    def retain(self) -> "ReusableModelGeneration":
        ...


@dataclass(frozen=True, slots=True)
class ReusedModelGeneration:
    handle: ReusableModelGeneration


@dataclass(frozen=True, slots=True)
class NewModelGeneration:
    compiled: CompiledModelGeneration
    reuse_key: ProductModelGenerationReuseKey


ModelGenerationBuildResult = ReusedModelGeneration | NewModelGeneration


class ModelHandleCloseError(RuntimeError):
    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        super().__init__(f"failed to close {len(errors)} model credential handle(s)")
        self.errors = errors


async def _close_handles(handles: dict[str, SecretHandle]) -> None:
    errors: list[BaseException] = []
    closed: set[int] = set()
    for handle in handles.values():
        if id(handle) in closed:
            continue
        closed.add(id(handle))
        try:
            await handle.aclose()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise ModelHandleCloseError(tuple(errors))


async def build_or_reuse_model_generation(
    source: ProductModelsConfig,
    *,
    provider_catalog_revision: ProviderCatalogRevision,
    adapter_factory_revision: AdapterFactoryRevision,
    credential_sources: CredentialSourceCatalog,
    current: ReusableModelGeneration | None,
    inference_revision: str,
) -> ModelGenerationBuildResult:
    plan = prepare_model_generation(
        source,
        provider_catalog_revision=provider_catalog_revision,
        adapter_factory_revision=adapter_factory_revision,
        credential_sources=credential_sources,
    )
    reuse_key = ProductModelGenerationReuseKey(plan.reuse_key, inference_revision)
    if current is not None and current.reuse_key == reuse_key:
        return ReusedModelGeneration(current.retain())
    handles: dict[str, SecretHandle] = {}
    try:
        for slot_id, endpoint_id, source_id in plan.slots:
            handles[slot_id] = await credential_sources.create_handle(
                slot_id,
                endpoint_id,
                source_id,
            )
        compiled = compile_model_generation(plan, handles)
        return NewModelGeneration(compiled, reuse_key)
    except BaseException as primary:
        try:
            await _close_handles(handles)
        except ModelHandleCloseError as close_error:
            raise close_error from primary
        raise


__all__ = [
    "ModelGenerationBuildResult",
    "ModelHandleCloseError",
    "NewModelGeneration",
    "ProductModelGenerationReuseKey",
    "ReusableModelGeneration",
    "ReusedModelGeneration",
    "build_or_reuse_model_generation",
]
