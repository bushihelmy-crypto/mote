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

import inspect
from typing import Any, Callable, Optional


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


def _is_output(obj: Any) -> bool:
    """True if *obj* is the :class:`Output` marker (the class or an instance)."""
    return obj is Output or isinstance(obj, Output)


def derive_output_fields(schema: type) -> set[str]:
    """Names of fields carrying the :class:`Output` marker in their metadata.

    Scans ``schema.model_fields[name].metadata`` the same way
    :func:`derive_reducers` does. An empty set means the schema declares no
    output, and callers fall back to returning the whole state (back-compat).
    """
    fields: set[str] = set()
    model_fields = getattr(schema, "model_fields", {})
    for name, field_info in model_fields.items():
        if any(_is_output(meta) for meta in getattr(field_info, "metadata", ())):
            fields.add(name)
    return fields


def _is_reducer(obj: Any) -> bool:
    """True if *obj* is a reducer: a callable taking exactly 2 positional args.

    A reducer has the shape ``(current, update) -> merged``. We probe the
    signature for exactly two parameters that can be passed positionally
    (POSITIONAL_ONLY / POSITIONAL_OR_KEYWORD), mirroring langgraph's
    ``_is_field_binop``. Builtins / C callables whose signature cannot be
    introspected (``inspect.signature`` raises ``ValueError``/``TypeError``)
    are conservatively treated as non-reducers.
    """
    if not callable(obj):
        return False
    try:
        sig = inspect.signature(obj)
    except (ValueError, TypeError):
        return False
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) == 2


def derive_reducers(schema: type) -> dict[str, Callable]:
    """Map field name → reducer for every ``Annotated[T, reducer]`` field.

    Scans ``schema.model_fields[name].metadata`` and takes the first entry that
    looks like a reducer (:func:`_is_reducer`). Fields without a reducer in
    their metadata are omitted — they are last-value channels.
    """
    reducers: dict[str, Callable] = {}
    model_fields = getattr(schema, "model_fields", {})
    for name, field_info in model_fields.items():
        for meta in getattr(field_info, "metadata", ()):
            if _is_reducer(meta):
                reducers[name] = meta
                break
    return reducers


def apply_updates(state: Any, updates: dict, reducers: Optional[dict] = None) -> None:
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
            setattr(state, key, reducer(current, value))
        else:
            setattr(state, key, value)
