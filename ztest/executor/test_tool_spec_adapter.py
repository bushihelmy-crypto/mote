#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.kernel.tools.spec_adapter``.

Builds JSON Schema from a ``call()`` signature + docstring and wraps it into the
provider envelope. Note ``from __future__ import annotations`` stringizes
annotations — ``build_json_schema`` resolves them via ``get_type_hints``, which
these tests exercise.
"""
from __future__ import annotations

import inspect
import pathlib
from typing import Optional, Union

from pydantic import BaseModel

from mote.kernel.tools.spec_adapter import _json_type, _unwrap_optional, build_json_schema, to_native_tool_specs


class Item(BaseModel):
    label: str
    qty: int = 1


class BatchItem(BaseModel):
    item: Item


class TestUnwrapOptional:
    def test_optional_strips_none(self):
        inner, is_opt = _unwrap_optional(Optional[int])
        assert inner is int
        assert is_opt is True

    def test_non_optional_unchanged(self):
        inner, is_opt = _unwrap_optional(int)
        assert inner is int
        assert is_opt is False

    def test_union_keeps_first_non_none(self):
        inner, is_opt = _unwrap_optional(Union[str, int, None])
        assert inner is str
        assert is_opt is True


class TestJsonType:
    def test_scalars(self):
        assert _json_type(str) == {"type": "string"}
        assert _json_type(int) == {"type": "integer"}
        assert _json_type(float) == {"type": "number"}
        assert _json_type(bool) == {"type": "boolean"}

    def test_list_with_item_type(self):
        assert _json_type(list[str]) == {"type": "array", "items": {"type": "string"}}

    def test_bare_dict_is_object(self):
        assert _json_type(dict[str, int]) == {"type": "object"}

    def test_unknown_falls_back_to_string(self):
        assert _json_type(pathlib.Path) == {"type": "string"}

    def test_empty_annotation_is_string(self):
        assert _json_type(inspect.Parameter.empty) == {"type": "string"}

    def test_pydantic_model_expands(self):
        schema = _json_type(Item)
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"label", "qty"}
        assert schema["required"] == ["label"]


class TestBuildJsonSchema:
    def test_scalar_params_with_required(self):
        def call(self, *, a: int, b: str = "x"):  # noqa: ANN001
            """Do.

            Args:
                a: the a.
                b: the b.
            """

        schema = build_json_schema(call)
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"a", "b"}
        assert schema["required"] == ["a"]
        # Descriptions come from the Args block.
        assert schema["properties"]["a"]["description"] == "the a."

    def test_skips_self_and_kwargs(self):
        def call(self, *args, real: str, **kwargs):  # noqa: ANN001
            ...

        schema = build_json_schema(call)
        assert set(schema["properties"]) == {"real"}

    def test_no_params_yields_empty_properties(self):
        def call(self):  # noqa: ANN001
            ...

        schema = build_json_schema(call)
        assert schema == {"type": "object", "properties": {}}

    def test_optional_param_not_required(self):
        def call(self, *, maybe: Optional[int] = None):  # noqa: ANN001
            ...

        schema = build_json_schema(call)
        assert "required" not in schema
        assert schema["properties"]["maybe"]["type"] == "integer"

    def test_list_of_models_expands(self):
        def call(self, *, items: list[Item]):  # noqa: ANN001
            ...

        schema = build_json_schema(call)
        items = schema["properties"]["items"]
        assert items["type"] == "array"
        assert items["items"]["type"] == "object"

    def test_nested_model_definitions_are_hoisted_to_root(self):
        def call(self, *, items: list[BatchItem]):  # noqa: ANN001
            ...

        schema = build_json_schema(call)
        item_schema = schema["properties"]["items"]["items"]
        assert item_schema["properties"]["item"]["$ref"] == "#/$defs/Item"
        assert schema["$defs"]["Item"]["required"] == ["label"]
        assert "$defs" not in item_schema


class TestToNativeToolSpecs:
    def _schemas(self):
        return {
            "Foo": {
                "name": "Foo",
                "description": "does foo",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        }

    def test_anthropic_envelope(self):
        specs = to_native_tool_specs(self._schemas(), provider="anthropic")
        assert specs == [
            {
                "name": "Foo",
                "description": "does foo",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        ]

    def test_openai_envelope(self):
        specs = to_native_tool_specs(self._schemas(), provider="openai")
        assert specs[0]["type"] == "function"
        fn = specs[0]["function"]
        assert fn["name"] == "Foo"
        assert fn["description"] == "does foo"
        assert fn["parameters"]["properties"] == {"x": {"type": "string"}}

    def test_provider_case_insensitive(self):
        specs = to_native_tool_specs(self._schemas(), provider="ANTHROPIC")
        assert "input_schema" in specs[0]

    def test_missing_input_schema_defaults_to_empty_object(self):
        schemas = {"Bar": {"name": "Bar", "description": ""}}
        specs = to_native_tool_specs(schemas, provider="anthropic")
        assert specs[0]["input_schema"] == {"type": "object", "properties": {}}


class TestJsonSchemaTransformer:
    """Per-model ``json_schema_transformer`` rewrites each tool schema pre-envelope."""

    def _schemas(self):
        return {
            "Foo": {
                "name": "Foo",
                "description": "does foo",
                "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        }

    def test_no_model_is_identity(self):
        # model=None → no transformer resolved → wire shape unchanged.
        specs = to_native_tool_specs(self._schemas(), provider="anthropic", model=None)
        assert specs[0]["input_schema"] == {"type": "object", "properties": {"x": {"type": "string"}}}

    def test_model_without_transformer_is_identity(self):
        specs = to_native_tool_specs(self._schemas(), provider="anthropic", model="claude-opus-4-8")
        assert specs[0]["input_schema"] == {"type": "object", "properties": {"x": {"type": "string"}}}

    def test_transformer_applied_before_envelope(self, monkeypatch):
        from mote.contracts.models import profile as model_profile

        def _strip_additional(schema: dict) -> dict:
            out = {k: v for k, v in schema.items()}
            out["additionalProperties"] = False
            return out

        fragment = model_profile.ModelProfile(json_schema_transformer=_strip_additional)
        monkeypatch.setattr(
            model_profile,
            "_PROFILE_REGISTRY",
            [*model_profile._PROFILE_REGISTRY, ("quirkmodel", fragment)],
        )

        specs = to_native_tool_specs(self._schemas(), provider="anthropic", model="quirkmodel-1")
        # Transformer ran, and its output landed inside the anthropic envelope.
        assert specs[0]["input_schema"]["additionalProperties"] is False
        assert specs[0]["input_schema"]["properties"] == {"x": {"type": "string"}}

    def test_transformer_applied_for_openai_envelope(self, monkeypatch):
        from mote.contracts.models import profile as model_profile

        fragment = model_profile.ModelProfile(json_schema_transformer=lambda s: {**s, "marked": True})
        monkeypatch.setattr(
            model_profile,
            "_PROFILE_REGISTRY",
            [*model_profile._PROFILE_REGISTRY, ("quirkmodel", fragment)],
        )
        specs = to_native_tool_specs(self._schemas(), provider="openai", model="quirkmodel-1")
        assert specs[0]["function"]["parameters"]["marked"] is True
