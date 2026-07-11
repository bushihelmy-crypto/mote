#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config reporting / CLI: human-readable dump of the layer stack.

This is the top of the config-center DAG (the L4 façade): it composes the
loader (L3), the typed :class:`Config` root model (L1) and the pure
schema-walk in :mod:`.diagnostics` (L2) to answer "where did this value come
from?". Because every dependency points strictly downward, all imports live at
module top level (no lazy-import cycle).

- :func:`format_report` — dump the layer stack, per-value provenance, and
  unknown keys (secrets redacted).
- ``python -m mote.common.config.report`` — the CLI entry point.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from mote.common.config.diagnostics import _get_path, _render_value, unknown_key_paths
from mote.common.config.loader import build_layer_stack
from mote.common.config.meta_config import Config
from mote.common.exception import UnknownConfigKeysError


def format_report(cwd: Optional[Path] = None, *, profile: Optional[str] = None) -> str:
    """Human-readable dump: layer stack + per-value provenance + unknown keys."""
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
    parser = argparse.ArgumentParser(
        prog="python -m mote.common.config.report",
        description="Dump the effective config, its provenance, and unknown keys.",
    )
    parser.add_argument("--cwd", default=None, help="working directory for layer discovery")
    parser.add_argument("--profile", default=None, help="named profile overlay to apply")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if unknown keys exist")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd) if args.cwd else None
    print(format_report(cwd, profile=args.profile))

    if args.strict:
        merged = build_layer_stack(cwd, profile=args.profile).effective()
        unknown = unknown_key_paths(merged, Config)
        if unknown:
            print("\n" + str(UnknownConfigKeysError(unknown)))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
