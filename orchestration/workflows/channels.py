"""State channels & reducers for :mod:`mote.runtime.tools.bggraph`.

LangGraph-style state sync: a node returns a ``dict`` of field updates
(``{field: value}``) instead of a bare value stored under its own name. Each
declared state field is a *channel*; an ``Annotated[T, reducer]`` field merges
contributions through the reducer (e.g. ``operator.add`` appends), while a plain
field is last-value (the most recent write wins).

This module is intentionally dependency-light (stdlib + pydantic only, like
``types.py``) so it can be imported by the engine and graph builder without
pulling in heavier layers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from pydantic import BaseModel

ValueT = TypeVar("ValueT")


class Output:
    """Marker for a state field that is part of the graph's *declared output*.

    Attach it in a field's ``Annotated`` metadata, exactly like a reducer::

        class ReviewState(GraphState):
            report: Annotated[str, Output] = ""   # the graph's result
            raw_diff: str = ""                    # intermediate, not returned

    On success the engine returns only the ``Output``-marked fields (see
    :func:`derive_output_fields` / ``_collect_finish_result``) instead of the
    whole state, so inputs and intermediate scratch never leak into the result
    pushed to the model. It is a bare sentinel (no config) so ``Annotated[T,
    Output]`` and ``Annotated[T, Output()]`` both work — :func:`_is_output`
    accepts the class or an instance.
    """


class NoOutput:
    """Explicit declaration that a workflow returns no state payload."""


@dataclass(frozen=True, slots=True)
class Reducer(Generic[ValueT]):
    """Explicit typed state-channel merge binding."""

    merge: Callable[[ValueT, ValueT], ValueT]

    def __call__(self, current: ValueT, update: ValueT) -> ValueT:
        return self.merge(current, update)

    def bind_channel(self) -> "ChannelReducer":
        """Erase one validated reducer only at the heterogeneous state map."""

        return ChannelReducer(cast(Callable[[object, object], object], self.merge))


@dataclass(frozen=True, slots=True)
class ChannelReducer:
    """Owner-private relation-preserving binding for a heterogeneous channel."""

    _merge: Callable[[object, object], object]

    def merge(self, current: object, update: object) -> object:
        return self._merge(current, update)


def _is_output(obj: object) -> bool:
    """True if *obj* is the :class:`Output` marker (the class or an instance)."""
    return obj is Output or isinstance(obj, Output)


def derive_output_fields(schema: type[BaseModel]) -> set[str]:
    """Names of fields carrying the :class:`Output` marker in their metadata.

    Scans ``schema.model_fields[name].metadata`` the same way
    :func:`derive_reducers` does. An empty set means the schema declares no
    output; compilation requires an explicit ``NoOutput`` declaration otherwise.
    """
    fields: set[str] = set()
    model_fields = schema.model_fields
    for name, field_info in model_fields.items():
        if any(_is_output(meta) for meta in field_info.metadata):
            fields.add(name)
    return fields


def derive_reducers(schema: type[BaseModel]) -> dict[str, ChannelReducer]:
    """Map field name → reducer for every ``Annotated[T, reducer]`` field.

    Scans ``schema.model_fields[name].metadata`` and takes the first entry that
    looks like a reducer (:func:`_is_reducer`). Fields without a reducer in
    their metadata are omitted — they are last-value channels.
    """
    reducers: dict[str, ChannelReducer] = {}
    model_fields = schema.model_fields
    for name, field_info in model_fields.items():
        for meta in field_info.metadata:
            if isinstance(meta, Reducer):
                reducers[name] = meta.bind_channel()
                break
    return reducers


def apply_updates(
    state: BaseModel,
    updates: Mapping[str, object],
    reducers: Mapping[str, ChannelReducer] | None = None,
) -> None:
    """Merge a node's *updates* dict into *state* in place.

    For each ``key, value`` in *updates*:
      * if ``key`` has a reducer → ``setattr(state, key, reducer(current, value))``
        where ``current = getattr(state, key, None)``;
      * otherwise → last-value ``setattr(state, key, value)``.
    """
    reducers = reducers or {}
    for key, value in updates.items():
        reducer = reducers.get(key)
        if reducer is not None:
            current = getattr(state, key, None)
            setattr(state, key, reducer.merge(current, value))
        else:
            setattr(state, key, value)
