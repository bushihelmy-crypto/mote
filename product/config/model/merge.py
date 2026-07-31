"""Mode-aware merge for the Product-owned model input subtree."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, get_args, get_origin

from mote.product.config.merge_primitives import deep_merge
from mote.product.config.model.inputs import ExplicitModelsConfig, ShortcutModelsConfig

_SHORTCUT_ONLY = frozenset({"default", "tasks", "recovery_defaults", "api_key_helper"})
_EXPLICIT_ONLY = frozenset({"endpoints", "credential_pools", "failover_groups", "routes", "recovery_profiles"})


@dataclass(frozen=True, slots=True)
class ModelLayer:
    source: str
    data: Mapping[str, Any]
    trusted: bool = True
    display_source: str | None = None


@dataclass(frozen=True, slots=True)
class ModelMergeResult:
    data: dict[str, Any]
    provenance: dict[str, str]


def _variant_fields(mode: str) -> frozenset[str]:
    model = ShortcutModelsConfig if mode == "shortcut" else ExplicitModelsConfig
    return frozenset(model.model_fields)


def _field_sensitive(field: Any) -> bool:
    return bool((field.json_schema_extra or {}).get("sensitive"))


def _strip_annotation(annotation: Any, value: Any) -> Any:
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        return _strip_model(annotation, value)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is dict and isinstance(value, dict) and len(arguments) == 2:
        return {key: _strip_annotation(arguments[1], item) for key, item in value.items()}
    if origin in (list, tuple) and isinstance(value, (list, tuple)) and arguments:
        return [_strip_annotation(arguments[0], item) for item in value]
    if arguments:
        for argument in arguments:
            if isinstance(argument, type) and hasattr(argument, "model_fields") and isinstance(value, dict):
                return _strip_model(argument, value)
    return deepcopy(value)


def _strip_model(model_type: type, value: Any) -> Any:
    if not isinstance(value, dict):
        return deepcopy(value)
    stripped: dict[str, Any] = {}
    for key, item in value.items():
        field = model_type.model_fields.get(key)
        if field is None or _field_sensitive(field) or (field.json_schema_extra or {}).get("untrusted_forbidden"):
            continue
        stripped[key] = _strip_annotation(field.annotation, item)
    return stripped


def strip_untrusted_model_credentials(models: Mapping[str, Any]) -> dict[str, Any]:
    mode = models.get("mode")
    model_type = ExplicitModelsConfig if mode == "explicit" else ShortcutModelsConfig
    return _strip_model(model_type, dict(models))


def _record_provenance(value: Any, source: str, result: dict[str, str], prefix: str = "models") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _record_provenance(item, source, result, f"{prefix}.{key}")
    else:
        result[prefix] = source


def merge_product_model_layers(
    layers: Iterable[ModelLayer],
) -> ModelMergeResult:
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    current_mode: str | None = None
    for layer in layers:
        display_source = layer.display_source or layer.source
        raw = deepcopy(dict(layer.data))
        if "mode" not in raw:
            if current_mode is None:
                raise ValueError(f"{display_source}: models.mode is required")
            else:
                mode = current_mode
        else:
            mode = raw["mode"]
        if mode not in ("shortcut", "explicit"):
            raise ValueError(f"{display_source}: unsupported models.mode {mode!r}")
        if not layer.trusted:
            raw = strip_untrusted_model_credentials(raw)
            raw["mode"] = mode
        incompatible = (set(raw) & _EXPLICIT_ONLY) if mode == "shortcut" else (set(raw) & _SHORTCUT_ONLY)
        if incompatible:
            raise ValueError(f"{display_source}: {mode} mode rejects fields {sorted(incompatible)!r}")
        if current_mode is None or mode != current_mode:
            merged = raw
            provenance = {key: value for key, value in provenance.items() if not key.startswith("models")}
        else:
            unknown = set(raw) - _variant_fields(mode)
            if unknown:
                raise ValueError(f"{display_source}: unknown {mode} fields {sorted(unknown)!r}")
            merged = deep_merge(merged, raw)
        _record_provenance(raw, layer.source, provenance)
        current_mode = mode
    return ModelMergeResult(merged, provenance)


__all__ = [
    "ModelLayer",
    "ModelMergeResult",
    "merge_product_model_layers",
    "strip_untrusted_model_credentials",
]
