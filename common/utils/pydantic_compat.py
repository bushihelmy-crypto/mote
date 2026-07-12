from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

TModel = TypeVar("TModel", bound=BaseModel)


def _strip_v2_only_dump_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    filtered = dict(kwargs)
    filtered.pop("mode", None)
    filtered.pop("warnings", None)
    return filtered


def model_dump(model: BaseModel, **kwargs) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**_strip_v2_only_dump_kwargs(kwargs))


def model_dump_json(model: BaseModel, **kwargs) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json(**kwargs)
    return model.json(**_strip_v2_only_dump_kwargs(kwargs))


def model_validate(model_cls: type[TModel], data: Any) -> TModel:
    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(data)
    if hasattr(model_cls, "parse_obj"):
        return model_cls.parse_obj(data)
    return model_cls(**data)


def model_validate_json(model_cls: type[TModel], data: str | bytes) -> TModel:
    if hasattr(model_cls, "model_validate_json"):
        return model_cls.model_validate_json(data)
    if hasattr(model_cls, "parse_raw"):
        return model_cls.parse_raw(data)
    return model_validate(model_cls, data)
