"""Per-run and per-step dynamic Toolsets with protocol-explicit factories."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Generic, TypeAlias, TypeVar, cast

from mote.contracts.tool import ToolsetProtocolError
from mote.kernel.execution.run_context import RunContext
from mote.runtime.tools.provider import NativeToolset, XmlToolset
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition

AgentDepsT = TypeVar("AgentDepsT")
SyncXmlToolsetFactory: TypeAlias = Callable[
    [RunContext[AgentDepsT]],
    XmlToolset[AgentDepsT] | None,
]
AsyncXmlToolsetFactory: TypeAlias = Callable[[RunContext[AgentDepsT]], Awaitable[XmlToolset[AgentDepsT] | None]]
XmlToolsetFactory: TypeAlias = SyncXmlToolsetFactory[AgentDepsT] | AsyncXmlToolsetFactory[AgentDepsT]
SyncNativeToolsetFactory: TypeAlias = Callable[
    [RunContext[AgentDepsT]],
    NativeToolset[AgentDepsT] | None,
]
AsyncNativeToolsetFactory: TypeAlias = Callable[[RunContext[AgentDepsT]], Awaitable[NativeToolset[AgentDepsT] | None]]
NativeToolsetFactory: TypeAlias = SyncNativeToolsetFactory[AgentDepsT] | AsyncNativeToolsetFactory[AgentDepsT]


def _bind_xml_factory(factory: XmlToolsetFactory[AgentDepsT]) -> AsyncXmlToolsetFactory[AgentDepsT]:
    if inspect.iscoroutinefunction(factory):
        return cast(AsyncXmlToolsetFactory[AgentDepsT], factory)
    sync_factory = cast(SyncXmlToolsetFactory[AgentDepsT], factory)

    async def invoke(ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT] | None:
        return sync_factory(ctx)

    return invoke


def _bind_native_factory(factory: NativeToolsetFactory[AgentDepsT]) -> AsyncNativeToolsetFactory[AgentDepsT]:
    if inspect.iscoroutinefunction(factory):
        return cast(AsyncNativeToolsetFactory[AgentDepsT], factory)
    sync_factory = cast(SyncNativeToolsetFactory[AgentDepsT], factory)

    async def invoke(ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT] | None:
        return sync_factory(ctx)

    return invoke


class XmlDynamicToolset(XmlToolset[AgentDepsT], Generic[AgentDepsT]):
    """Build an XML Toolset from typed run dependencies."""

    def __init__(
        self,
        id: str,
        factory: XmlToolsetFactory[AgentDepsT],
        *,
        version: str = "1",
        per_run_step: bool = False,
    ) -> None:
        self._factory = _bind_xml_factory(factory)
        self._per_run_step = per_run_step
        self._inner: XmlToolset[AgentDepsT] | None = None
        self._entered = False
        super().__init__(
            id,
            self._active_definitions,
            version=version,
            requires_permission_gate=True,
        )

    def _active_definitions(self) -> Iterable[XmlToolDefinition]:
        return () if self._inner is None else self._inner.definitions()

    @property
    def changes_per_run_step(self) -> bool:
        return self._per_run_step

    @property
    def run_scoped_lifecycle(self) -> bool:
        return True

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return () if self._inner is None else self._inner.instruction_blocks

    async def _evaluate(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT] | None:
        result = await self._factory(ctx)
        if result is None:
            return None
        if not isinstance(result, XmlToolset):
            raise ToolsetProtocolError(
                f"XML dynamic Toolset {self.id!r} returned {type(result).__name__}; " "expected XmlToolset or None"
            )
        if result is self:
            raise RuntimeError(f"dynamic Toolset {self.id!r} cannot return itself")
        return await result.for_run(ctx)

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        active = XmlDynamicToolset(
            self.id,
            self._factory,
            version=self.version,
            per_run_step=self._per_run_step,
        )
        if not self._per_run_step:
            active._inner = await active._evaluate(ctx)
        active._bind_run_context(ctx)
        return active

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> XmlToolset[AgentDepsT]:
        if not self._per_run_step:
            return self
        inner = await self._evaluate(ctx)
        if inner is self._inner:
            return self
        previous, self._inner = self._inner, None
        if previous is not None and self._entered:
            await previous.__aexit__(None, None, None)
        if inner is not None and self._entered:
            await inner.__aenter__()
        self._inner = inner
        return self

    async def __aenter__(self) -> XmlToolset[AgentDepsT]:
        if self._entered:
            raise RuntimeError(f"dynamic Toolset {self.id!r} is already active")
        if self._inner is not None:
            await self._inner.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        try:
            if self._inner is not None:
                return await self._inner.__aexit__(*_exc)
            return None
        finally:
            self._inner = None
            self._entered = False


class NativeDynamicToolset(NativeToolset[AgentDepsT], Generic[AgentDepsT]):
    """Build a Native Toolset from typed run dependencies."""

    def __init__(
        self,
        id: str,
        factory: NativeToolsetFactory[AgentDepsT],
        *,
        version: str = "1",
        per_run_step: bool = False,
    ) -> None:
        self._factory = _bind_native_factory(factory)
        self._per_run_step = per_run_step
        self._inner: NativeToolset[AgentDepsT] | None = None
        self._entered = False
        super().__init__(
            id,
            self._active_definitions,
            version=version,
            requires_permission_gate=True,
        )

    def _active_definitions(self) -> Iterable[NativeToolDefinition]:
        return () if self._inner is None else self._inner.definitions()

    @property
    def changes_per_run_step(self) -> bool:
        return self._per_run_step

    @property
    def run_scoped_lifecycle(self) -> bool:
        return True

    @property
    def dynamic_instruction_blocks(self) -> tuple[str, ...]:
        return () if self._inner is None else self._inner.instruction_blocks

    async def _evaluate(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT] | None:
        result = await self._factory(ctx)
        if result is None:
            return None
        if not isinstance(result, NativeToolset):
            raise ToolsetProtocolError(
                f"Native dynamic Toolset {self.id!r} returned {type(result).__name__}; "
                "expected NativeToolset or None"
            )
        if result is self:
            raise RuntimeError(f"dynamic Toolset {self.id!r} cannot return itself")
        return await result.for_run(ctx)

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        active = NativeDynamicToolset(
            self.id,
            self._factory,
            version=self.version,
            per_run_step=self._per_run_step,
        )
        if not self._per_run_step:
            active._inner = await active._evaluate(ctx)
        active._bind_run_context(ctx)
        return active

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> NativeToolset[AgentDepsT]:
        if not self._per_run_step:
            return self
        inner = await self._evaluate(ctx)
        if inner is self._inner:
            return self
        previous, self._inner = self._inner, None
        if previous is not None and self._entered:
            await previous.__aexit__(None, None, None)
        if inner is not None and self._entered:
            await inner.__aenter__()
        self._inner = inner
        return self

    async def __aenter__(self) -> NativeToolset[AgentDepsT]:
        if self._entered:
            raise RuntimeError(f"dynamic Toolset {self.id!r} is already active")
        if self._inner is not None:
            await self._inner.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, *_exc: object) -> bool | None:
        try:
            if self._inner is not None:
                return await self._inner.__aexit__(*_exc)
            return None
        finally:
            self._inner = None
            self._entered = False


__all__ = [
    "NativeDynamicToolset",
    "NativeToolsetFactory",
    "XmlDynamicToolset",
    "XmlToolsetFactory",
]
