"""Side-effect-free discovery of layered ``.mote`` paths."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mote.product.paths.defaults import default_runtime_paths
from mote.runtime.vcs.probe import find_git_root

MOTE_DIR_NAME = ".mote"


def user_mote_dir(subdir: str, *, user_config_root: Path | None = None) -> Path:
    root = user_config_root or default_runtime_paths().user_config_root
    return root / subdir


def _discovery_roots(cwd: Optional[Path]) -> list[Path]:
    start = Path(cwd) if cwd is not None else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()
    git_root = find_git_root(str(start))
    stop = Path(git_root).resolve() if git_root else None
    roots: list[Path] = []
    current = start
    while True:
        roots.append(current)
        if stop is not None and current == stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    roots.reverse()
    return roots


def mote_project_dirs(subdir: str, cwd: Optional[Path] = None) -> List[Path]:
    return [candidate for root in _discovery_roots(cwd) if (candidate := root / MOTE_DIR_NAME / subdir).is_dir()]


def mote_project_files(filename: str, cwd: Optional[Path] = None) -> List[Path]:
    return [candidate for root in _discovery_roots(cwd) if (candidate := root / MOTE_DIR_NAME / filename).is_file()]


def mote_layered_files(
    filename: str,
    cwd: Optional[Path] = None,
    *,
    user_config_root: Path | None = None,
) -> List[Path]:
    root = user_config_root or default_runtime_paths().user_config_root
    user_file = root / filename
    return [
        *([user_file] if user_file.is_file() else []),
        *mote_project_files(filename, cwd),
    ]


def mote_source_dirs(
    subdir: str,
    cwd: Optional[Path] = None,
    *,
    user_config_root: Path | None = None,
) -> List[Path]:
    return [
        user_mote_dir(subdir, user_config_root=user_config_root),
        *mote_project_dirs(subdir, cwd),
    ]


__all__ = [
    "MOTE_DIR_NAME",
    "mote_layered_files",
    "mote_project_dirs",
    "mote_project_files",
    "mote_source_dirs",
    "user_mote_dir",
]
