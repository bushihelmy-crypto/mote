#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ComponentGraph — a lazy, cycle-checked build engine for a Role's subsystems.

The Role's collaborators (LLM router, tool executor, context manager, event bus,
session log, the opt-in hook/LSP/sandbox/file-watch layers, the per-turn context
bus, …) form a dependency graph: some are pure leaves, some read a handful of
siblings while building. A hand-written lazy ``@property`` per component (each
reading its siblings directly and caching into a slot) would leave nothing to
*structurally* prevent a construction cycle (a getter reading a sibling that
reads it back would stack-overflow or double-build) or a getter mutating a
sibling as a hidden side-effect.

This module uses one declarative registry and one resolver instead:

- A component is a :class:`ComponentSpec` — a ``name``, a pure ``build`` function
  of a :class:`BuildContext`, and an optional ``available`` predicate (opt-in
  layers return unavailable => ``None`` when their config is off).
- :class:`ComponentGraph` resolves each component lazily and caches it, and — the
  point of the exercise — maintains a *resolution stack* so a construction cycle
  raises :class:`ComponentCycleError` at the moment it's traversed, instead of
  recursing to a stack overflow. Wiring (runtime cross-references) is deliberately
  *not* the graph's job: builders return a value, they never reach back to mutate
  a sibling, so the build phase is a pure DAG and the cyclic wiring is layered on
  afterward by an explicit step.

Sibling access inside a builder goes through the :class:`BuildContext`:

- ``ctx.dep(name)`` resolves a sibling **now** — an *eager* edge that participates
  in cycle detection (it re-enters :meth:`ComponentGraph.get` with the current
  component on the stack).
- ``ctx.defer(name)`` returns a zero-arg thunk that resolves the sibling **on
  call** — a *deferred* edge (no build now), for collaborators that only need the
  sibling later (e.g. a roster feed that reads the context manager per turn), so
  assembling one component never forces another to build mid-resolve.

``None`` is never cached: an unavailable opt-in layer (or a builder that returns
``None`` because a precondition isn't met, e.g. the file-watch service with no
hook consumer) is re-evaluated on the next access, so a layer that gets engaged
later (``register_hook``) is picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


class ComponentGraphError(Exception):
    """Base class for build-graph errors."""


class UnknownComponentError(ComponentGraphError, KeyError):
    """Requested a component name with no registered spec."""


class ComponentCycleError(ComponentGraphError):
    """A component's construction transitively depends on itself.

    Raised the moment the resolver re-enters a name already on the resolution
    stack, with the offending path in the message — a clear, immediate failure
    instead of a stack overflow or a silently double-built component.
    """


@dataclass(frozen=True)
class ComponentSpec:
    """The declarative description of one buildable component.

    - ``name``: the registry key (also the Role's public attribute name).
    - ``build``: a pure function ``(BuildContext) -> value`` that constructs the
      component, reading siblings only through ``ctx.dep`` / ``ctx.defer`` and
      role facts through ``ctx.role`` / ``ctx.state``. Must not mutate a sibling
      (wiring is a separate, explicit lifecycle step).
    - ``available``: optional ``(role, state) -> bool`` gate for opt-in layers;
      when it returns ``False`` the component resolves to ``None`` (not cached).
    """

    name: str
    build: Callable[["BuildContext"], Any]
    available: Optional[Callable[[Any, Any], bool]] = None


class BuildContext:
    """The narrow surface a :class:`ComponentSpec.build` sees while constructing.

    Exposes the role and its mutable extras (``state``) plus the two sibling-edge
    primitives — ``dep`` (eager, cycle-tracked) and ``defer`` (lazy thunk). A
    builder never touches the graph directly: the resolver is *closed over* by
    ``dep``/``defer`` rather than stored as an attribute, so there is no
    ``ctx``-reachable path to :meth:`ComponentGraph.get` / ``seed`` / ``_slots``.
    That turns the "only reach a sibling through an edge the resolver can see"
    rule from a documented convention into a *structural* one — a builder
    physically cannot pull a sibling off-book (bypassing cycle detection and the
    availability gates) or dirty-write a slot mid-construction. ``__slots__``
    also forbids stashing arbitrary attributes on the context.
    """

    __slots__ = ("role", "state", "dep", "defer")

    def __init__(self, graph: "ComponentGraph", role: Any, state: Any):
        self.role = role
        self.state = state

        # ``dep`` / ``defer`` close over the resolver so it is never an attribute
        # on ``ctx``: a builder gets exactly these two edge primitives and no
        # other path back to ``get`` / ``seed`` / ``_slots``.
        def dep(name: str) -> Any:
            """Resolve sibling ``name`` now — an eager edge, cycle-tracked (it
            re-enters the resolver with this component on the stack)."""
            return graph.get(name)

        def defer(name: str) -> Callable[[], Any]:
            """Return a thunk resolving sibling ``name`` on call — a deferred edge
            (no build now), for a sibling only read later."""
            return lambda: graph.get(name)

        self.dep = dep
        self.defer = defer


class ComponentGraph:
    """Lazy, cycle-checked resolver over a set of :class:`ComponentSpec`.

    ``get(name)`` returns the (cached) component, building it — and, transitively,
    any sibling its builder pulls via ``ctx.dep`` — on first access. A build cycle
    raises :class:`ComponentCycleError`. ``peek(name)`` returns the built value or
    ``None`` without triggering a build (for teardown / "look, don't create"
    paths).
    """

    def __init__(self, role: Any, state: Any, specs: Iterable[ComponentSpec]):
        self._role = role
        self._state = state
        self._specs: dict[str, ComponentSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ComponentGraphError(f"Duplicate component spec: {spec.name!r}")
            self._specs[spec.name] = spec
        self._slots: dict[str, Any] = {}
        self._resolving: list[str] = []

    def get(self, name: str) -> Any:
        """Resolve (and cache) component ``name``; ``None`` for an unavailable layer."""
        if name in self._slots:
            return self._slots[name]

        try:
            spec = self._specs[name]
        except KeyError:
            raise UnknownComponentError(name) from None

        # Opt-in gate: an unavailable layer resolves to None and is NOT cached, so
        # a later engagement (e.g. register_hook flipping the hook layer on) is
        # picked up on the next access.
        if spec.available is not None and not spec.available(self._role, self._state):
            return None

        if name in self._resolving:
            path = " -> ".join([*self._resolving, name])
            raise ComponentCycleError(f"Construction cycle detected: {path}")

        self._resolving.append(name)
        try:
            value = spec.build(BuildContext(self, self._role, self._state))
        finally:
            self._resolving.pop()

        # None (a precondition unmet, e.g. file-watch with no consumer) is not
        # cached — mirrors the historic "slot stays None => rebuilt next time".
        if value is not None:
            self._slots[name] = value
        return value

    def peek(self, name: str) -> Any:
        """The built component if it already exists, else ``None`` (no build)."""
        if name not in self._specs:
            raise UnknownComponentError(name)
        return self._slots.get(name)

    def is_built(self, name: str) -> bool:
        """Whether ``name`` has a cached (non-None) component."""
        return name in self._slots

    def seed(self, name: str, value: Any) -> None:
        """Pre-populate a slot, bypassing the builder (test/DI injection seam).

        Stamps ``value`` into the cache for ``name`` (which must be a registered
        component) so a later :meth:`get` returns it without building — the way a
        test injects a scripted/offline double (e.g. a no-network router) in
        place of the real collaborator, mirroring the old "assign the private
        slot" pattern but going through the one resolver.
        """
        if name not in self._specs:
            raise UnknownComponentError(name)
        self._slots[name] = value


__all__ = [
    "BuildContext",
    "ComponentSpec",
    "ComponentGraph",
    "ComponentGraphError",
    "ComponentCycleError",
    "UnknownComponentError",
]
