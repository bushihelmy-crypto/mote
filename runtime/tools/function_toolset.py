"""Protocol-explicit function Toolsets with least-authority dependency projection."""

from __future__ import annotations

import inspect
import types
from collections.abc import Callable, Mapping
from typing import Any, Generic, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from mote.contracts.introspection.docstrings import description_body, first_line
from mote.contracts.tools import NativeToolSchema, XmlToolSchema
from mote.kernel.tools.definitions import NativeToolDefinition, XmlToolDefinition
from mote.kernel.tools.spec_adapter import build_json_schema
from mote.kernel.tools.toolset import NativeToolset, XmlToolset
from mote.runtime.run_context import current_run_context
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.tool_convert import function_docstring_to_schema

AgentDepsT = TypeVar("AgentDepsT")
ToolDepsT = TypeVar("ToolDepsT")
ToolFuncT = TypeVar("ToolFuncT", bound=Callable[..., Any])
DepsProjector = Callable[[AgentDepsT], ToolDepsT]


def _model_callable(function: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(function)
    parameters = tuple(signature.parameters.values())
    if not parameters:
        raise TypeError("Function tools must accept ToolContext as their first parameter")
    context_parameter = parameters[0]
    if context_parameter.kind in {
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }:
        raise TypeError("Function tool context parameter must be explicit")

    async def model_callable(**kwargs: Any) -> Any:
        return kwargs

    model_callable.__name__ = function.__name__
    model_callable.__doc__ = function.__doc__
    model_callable.__signature__ = signature.replace(parameters=parameters[1:])  # type: ignore[attr-defined]
    model_callable.__annotations__ = {
        name: annotation
        for name, annotation in getattr(function, "__annotations__", {}).items()
        if name != context_parameter.name
    }
    return model_callable


def _xml_annotation_supported(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return set(get_args(annotation)) <= {str, type(None)}
    return False


def _validate_xml_signature(function: Callable[..., Any]) -> None:
    parameters = tuple(inspect.signature(function).parameters.values())[1:]
    annotations = get_type_hints(function)
    for parameter in parameters:
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            raise TypeError(f"XML function tool parameter {parameter.name!r} cannot be variadic")
        if not _xml_annotation_supported(annotations.get(parameter.name, parameter.annotation)):
            raise TypeError(
                f"XML function tool parameter {parameter.name!r} must be annotated as str or str | None; "
                "use NativeFunctionToolset for structured or typed JSON arguments"
            )


class _FunctionCapability(BaseTool):
    _function: Callable[..., Any]
    _project: Callable[[Any], Any]

    async def call(self, **kwargs: Any) -> Any:
        run_context = current_run_context()
        if run_context is None:
            raise RuntimeError(f"function tool '{self.name}' called outside an Agent run")
        tool_context = run_context.for_tool(self._project)
        result = self._function(tool_context, **kwargs)
        return await result if inspect.isawaitable(result) else result


def _capability_type(
    function: Callable[..., Any],
    project: Callable[[Any], Any],
    name: str,
) -> type[_FunctionCapability]:
    adapter_name = f"{function.__name__.title().replace('_', '')}FunctionCapability"
    return cast(
        type[_FunctionCapability],
        type(
            adapter_name,
            (_FunctionCapability,),
            {
                "name": name,
                "_function": staticmethod(function),
                "_project": staticmethod(project),
            },
        ),
    )


def _xml_function_definition(
    function: Callable[..., Any],
    project: Callable[[Any], Any],
    name: str,
) -> XmlToolDefinition[Any]:
    _validate_xml_signature(function)
    model_function = _model_callable(function)
    docstring = inspect.getdoc(model_function) or ""
    capability_type = _capability_type(function, project, name)

    def render(_capability: Any) -> XmlToolSchema:
        return {
            "name": name,
            "description": description_body(docstring),
            "parameters": function_docstring_to_schema(model_function, docstring),
        }

    summary = first_line(description_body(docstring))
    return XmlToolDefinition(
        name=name,
        capability_factory=capability_type,
        capability_type=capability_type,
        schema_renderer=render,
        description=description_body(docstring),
        summary=summary,
        search_text=summary,
    )


def _native_function_definition(
    function: Callable[..., Any],
    project: Callable[[Any], Any],
    name: str,
) -> NativeToolDefinition[Any]:
    model_function = _model_callable(function)
    docstring = inspect.getdoc(model_function) or ""
    capability_type = _capability_type(function, project, name)

    def render(_capability: Any) -> NativeToolSchema:
        return {
            "name": name,
            "description": description_body(docstring),
            "input_schema": build_json_schema(model_function),
        }

    summary = first_line(description_body(docstring))
    return NativeToolDefinition(
        name=name,
        capability_factory=capability_type,
        capability_type=capability_type,
        schema_renderer=render,
        description=description_body(docstring),
        summary=summary,
        search_text=summary,
    )


class XmlFunctionToolset(XmlToolset[AgentDepsT], Generic[AgentDepsT]):
    """Function tools explicitly registered for the scalar XML protocol."""

    def __init__(self, id: str, *, version: str = "1") -> None:
        self._registered: dict[str, XmlToolDefinition[Any]] = {}
        super().__init__(id, lambda: self._registered.values(), version=version)

    def tool(
        self,
        *,
        project: DepsProjector[AgentDepsT, ToolDepsT],
        name: str | None = None,
    ) -> Callable[[ToolFuncT], ToolFuncT]:
        def register(function: ToolFuncT) -> ToolFuncT:
            tool_name = _validated_name(name or function.__name__, self._registered)
            self._registered[tool_name] = _xml_function_definition(function, project, tool_name)
            return function

        return register


class NativeFunctionToolset(NativeToolset[AgentDepsT], Generic[AgentDepsT]):
    """Function tools explicitly registered for structured native tool use."""

    def __init__(self, id: str, *, version: str = "1") -> None:
        self._registered: dict[str, NativeToolDefinition[Any]] = {}
        super().__init__(id, lambda: self._registered.values(), version=version)

    def tool(
        self,
        *,
        project: DepsProjector[AgentDepsT, ToolDepsT],
        name: str | None = None,
    ) -> Callable[[ToolFuncT], ToolFuncT]:
        def register(function: ToolFuncT) -> ToolFuncT:
            tool_name = _validated_name(name or function.__name__, self._registered)
            self._registered[tool_name] = _native_function_definition(function, project, tool_name)
            return function

        return register


def _validated_name(name: str, registered: Mapping[str, object]) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("function tool name must not be empty")
    if normalized in registered:
        raise ValueError(f"duplicate function tool name: {normalized}")
    return normalized


__all__ = ["NativeFunctionToolset", "XmlFunctionToolset"]
