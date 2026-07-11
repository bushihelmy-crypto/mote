#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config diagnostics: the pure schema-walk half of the config center.

The typed :class:`Config` is lenient by default (pydantic ``extra='ignore'``):
unknown keys in any layer are silently dropped, which keeps forward/backward
compatibility but hides typos and stale keys. This module is the dependency-free
schema layer (L2 of the config DAG): it walks a merged dict against a pydantic
model *type* passed in by the caller, so it never imports the loader or the
``Config`` root model — keeping the config package a strict, lazy-import-free DAG.

- :func:`unknown_key_paths` — walk a merged dict against the model schema and
  report dotted paths that no field accepts (recursing into nested models). Used
  by the loader for strict mode (``load_config(..., strict=True)`` raises
  :class:`~mote.common.exception.UnknownConfigKeysError`).
- redaction helpers (:func:`_is_secret`, :func:`_render_value`) — shared with the
  reporting/CLI layer in :mod:`.report` (``python -m mote.common.config.report``).
"""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from mote.common.config.layers import CREDENTIAL_DENYLIST

# Substrings that mark a leaf as secret for the redacted dump.
_SECRET_HINTS = ("key", "secret", "token", "password", "jwt")


def _model_of(annotation: Any) -> Optional[type]:
    """Resolve a field annotation to a :class:`BaseModel` subclass, if any.

    Unwraps ``Optional[X]`` / ``Union[...]`` / generics so a nested model is
    found for recursion; returns ``None`` for plain scalars/containers.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        model = _model_of(arg)
        if model is not None:
            return model
    return None


def unknown_key_paths(data: Any, model: type, prefix: str = "") -> List[str]:
    """Return dotted paths in ``data`` not declared by the pydantic ``model``.

    Recurses into a key only when its declared annotation resolves to a nested
    :class:`BaseModel` and its value is a dict; unknown keys are reported but
    not descended into (their whole subtree is unknown).
    """
    if not isinstance(data, dict):
        return []
    known: Dict[str, Any] = {}
    for name, info in model.model_fields.items():
        known[name] = info
        if getattr(info, "alias", None):
            known[info.alias] = info

    unknown: List[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        info = known.get(key)
        if info is None:
            unknown.append(path)
            continue
        if isinstance(value, dict):
            sub = _model_of(info.annotation)
            if sub is not None:
                unknown.extend(unknown_key_paths(value, sub, path))
    return unknown


def _is_secret(dotted: str) -> bool:
    leaf = dotted.rsplit(".", 1)[-1].lower()
    if leaf in CREDENTIAL_DENYLIST:
        return True
    return any(hint in leaf for hint in _SECRET_HINTS)


def _get_path(data: Any, dotted: str) -> Any:
    cur = data
    for segment in dotted.split("."):
        if not isinstance(cur, dict) or segment not in cur:
            return None
        cur = cur[segment]
    return cur


def _render_value(dotted: str, value: Any) -> str:
    if _is_secret(dotted) and value not in (None, "", []):
        return "***"
    return repr(value)
