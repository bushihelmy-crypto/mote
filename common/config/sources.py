#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config layer sources and file discovery.

Defines the ordered set of places a configuration value may come from and how
each is resolved to concrete files on disk. The integer value of
:class:`ConfigSource` *is* the precedence (higher wins), so the merge order is
data, not branching logic (mirrors codex's ``ConfigLayerSource::precedence``).

All layers are wired: DEFAULT/SYSTEM/USER/PROJECT/WORKDIR/PROFILE/ENV/CLI_FLAG/
PROGRAMMATIC. Numeric gaps are left between values so future layers slot in
without renumbering existing ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import List, Optional

from mote.common.const import CONFIG_ROOT, SOURCE_ROOT

CONFIG_FILE_NAME = "config.yaml"
LEGACY_CONFIG_FILE_NAME = "config2.yaml"
PROFILE_FILE_SUFFIX = ".config.yaml"  # ~/.mote/<name>.config.yaml
MANAGED_CONFIG_FILE_NAME = "managed.config.yaml"  # /etc/mote/managed.config.yaml

_SYSTEM_CONFIG_DIR = Path("/etc/mote")
_USER_CONFIG_DIR = Path.home() / ".mote"
_WORKDIR_CONFIG_SUBDIR = ".mote"


class ConfigSource(IntEnum):
    """A config layer source. The integer value is the precedence (higher wins).

    Lower layers are overridden by higher ones during the merge. Gaps are left
    between values so future layers slot in without renumbering existing ones.
    """

    DEFAULT = 0  # pydantic field defaults (no file)
    SYSTEM = 10  # /etc/mote/config.yaml (+ config.d/*.yaml) — managed
    USER = 20  # ~/.mote/config.yaml (BC: ~/.mote/config2.yaml)
    PROJECT = 30  # trusted repo/installation config (mote/config.yaml)
    WORKDIR = 35  # <cwd>/.mote/config.yaml — UNTRUSTED, credentials stripped
    PROFILE = 40  # ~/.mote/<name>.config.yaml — named overlay, trusted
    ENV = 50  # MOTE_*/MOTE_* environment variables
    CLI_FLAG = 60  # -c key=value runtime overrides
    PROGRAMMATIC = 70  # code-supplied overrides
    MANAGED = 80  # /etc/mote/managed.config.yaml — admin policy, locks all below

    @property
    def trusted(self) -> bool:
        """Untrusted layers get credential-redirecting keys stripped on load.

        Only the working-directory layer is untrusted: it may come from an
        arbitrary checked-out repo, so it must not be able to redirect LLM
        credentials/endpoints (codex's project-local denylist carve-out).
        """
        return self is not ConfigSource.WORKDIR


@dataclass(frozen=True)
class SourceFile:
    """A concrete config file resolved to a particular source layer."""

    source: ConfigSource
    path: Path


def _existing(paths: List[Optional[Path]]) -> List[Path]:
    return [p for p in paths if p is not None and p.is_file()]


def profile_path(profile: str) -> Path:
    """The on-disk path of a named profile overlay (``~/.mote/<name>.config.yaml``)."""
    return _USER_CONFIG_DIR / f"{profile}{PROFILE_FILE_SUFFIX}"


def discover_source_files(cwd: Optional[Path] = None, *, profile: Optional[str] = None) -> List[SourceFile]:
    """Resolve every config file that exists, in ascending precedence order.

    A single source may map to multiple files (e.g. USER = the legacy
    ``~/.mote/config2.yaml`` plus ``~/.mote/config.yaml``); they are
    listed low->high so a later file overrides an earlier one within the same
    source band. The trusted PROJECT band is the user's ``mote/config.yaml``.

    When ``profile`` is given, ``~/.mote/<profile>.config.yaml`` is added
    as a trusted PROFILE layer (above WORKDIR, below ENV) — a named overlay
    that selectively overrides the base config (mirrors codex profiles).
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    files: List[SourceFile] = []

    # SYSTEM: base file then drop-in dir (config.d/*.yaml, sorted alphabetically).
    for p in _existing([_SYSTEM_CONFIG_DIR / CONFIG_FILE_NAME]):
        files.append(SourceFile(ConfigSource.SYSTEM, p))
    sys_d = _SYSTEM_CONFIG_DIR / "config.d"
    if sys_d.is_dir():
        for p in sorted(sys_d.glob("*.yaml")):
            files.append(SourceFile(ConfigSource.SYSTEM, p))

    # USER: legacy ~/.mote/config2.yaml (BC) then ~/.mote/config.yaml.
    for p in _existing([CONFIG_ROOT / LEGACY_CONFIG_FILE_NAME, _USER_CONFIG_DIR / CONFIG_FILE_NAME]):
        files.append(SourceFile(ConfigSource.USER, p))

    # PROJECT (trusted): the user's mote/config.yaml.
    for p in _existing([SOURCE_ROOT / CONFIG_FILE_NAME]):
        files.append(SourceFile(ConfigSource.PROJECT, p))

    # WORKDIR (untrusted): <cwd>/.mote/config.yaml.
    for p in _existing([cwd / _WORKDIR_CONFIG_SUBDIR / CONFIG_FILE_NAME]):
        files.append(SourceFile(ConfigSource.WORKDIR, p))

    # PROFILE (trusted): named overlay ~/.mote/<profile>.config.yaml.
    if profile:
        for p in _existing([profile_path(profile)]):
            files.append(SourceFile(ConfigSource.PROFILE, p))

    # MANAGED (trusted, highest): admin policy that overrides every other layer.
    for p in _existing([_SYSTEM_CONFIG_DIR / MANAGED_CONFIG_FILE_NAME]):
        files.append(SourceFile(ConfigSource.MANAGED, p))

    return files
