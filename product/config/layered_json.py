"""Best-effort JSON loading over paths supplied by composition code."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from mote.runtime.telemetry.logging import logger


def load_json_section(path: Path, top_key: str, log_prefix: str) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(f"{log_prefix}: could not read {path}: {exc}")
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"{log_prefix}: {path} is not valid JSON: {exc}")
        return {}
    section = data.get(top_key) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        logger.warning(f"{log_prefix}: {path} has no '{top_key}' object.")
        return {}
    return section


def existing_paths(paths: Iterable[Path]) -> list[Path]:
    return [path for path in paths if path.is_file()]


__all__ = ["existing_paths", "load_json_section"]
