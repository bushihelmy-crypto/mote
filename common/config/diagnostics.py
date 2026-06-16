#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config diagnostics: strict-mode unknown-key detection and a dump command.

The typed :class:`Config` is lenient by default (pydantic ``extra='ignore'``):
unknown keys in any layer are silently dropped, which keeps forward/backward
compatibility but hides typos and stale keys. This module provides:

- :func:`unknown_key_paths` — walk a merged dict against the model schema and
  report dotted paths that no field accepts (recursing into nested models).
- strict mode — :class:`~metagpt.common.exception.UnknownConfigKeysError`, raised
  by the loader when asked to validate strictly (``load_config(..., strict=True)``).
- :func:`format_report` / ``python -m metagpt.common.config.diagnostics`` — a
  human-readable dump of the layer stack, per-value provenance, and unknown
  keys (secrets redacted), answering "where did this value come from?".
"""
from __future__ import annotations

import typing
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from metagpt.common.config.layers import CREDENTIAL_DENYLIST
from metagpt.common.exception import UnknownConfigKeysError

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


def format_report(cwd: Optional[Path] = None, *, profile: Optional[str] = None) -> str:
    """Human-readable dump: layer stack + per-value provenance + unknown keys."""
    from metagpt.common.config.loader import build_layer_stack
    from metagpt.common.config.meta_config import Config

    stack = build_layer_stack(cwd, profile=profile)
    merged = stack.effective()
    prov = stack.provenance()
    unknown = unknown_key_paths(merged, Config)

    lines: List[str] = ["# Config layers (low -> high precedence)"]
    if stack.layers:
        for layer in stack.sorted_layers():
            loc = str(layer.path) if layer.path else "(in-memory)"
            lines.append(f"  [{int(layer.source):>3}] {layer.source.name:<12} {loc}")
    else:
        lines.append("  (no layers — pure defaults)")

    lines.append("")
    lines.append("# Effective values and their source")
    for path in sorted(prov):
        value = _render_value(path, _get_path(merged, path))
        lines.append(f"  {path} = {value}  [{prov[path]}]")

    if unknown:
        lines.append("")
        lines.append("# Unknown keys (ignored unless strict)")
        for path in unknown:
            lines.append(f"  {path}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m metagpt.common.config.diagnostics",
        description="Dump the effective config, its provenance, and unknown keys.",
    )
    parser.add_argument("--cwd", default=None, help="working directory for layer discovery")
    parser.add_argument("--profile", default=None, help="named profile overlay to apply")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if unknown keys exist")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd) if args.cwd else None
    print(format_report(cwd, profile=args.profile))

    if args.strict:
        from metagpt.common.config.loader import build_layer_stack
        from metagpt.common.config.meta_config import Config

        merged = build_layer_stack(cwd, profile=args.profile).effective()
        unknown = unknown_key_paths(merged, Config)
        if unknown:
            print("\n" + str(UnknownConfigKeysError(unknown)))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
