#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config layer sources and file discovery.

Defines the ordered set of places a configuration value may come from and how
each is resolved to concrete files on disk. The integer value of
:class:`ConfigSource` *is* the precedence (higher wins), so the merge order is
data, not branching logic (mirrors codex's ``ConfigLayerSource::precedence``).

All layers are wired: DEFAULT/SYSTEM/USER/WORKDIR/PROFILE/ENV/CLI_FLAG/
PROGRAMMATIC. Numeric gaps are left between values so future layers slot in
without renumbering existing ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import List, Optional, Sequence

CONFIG_FILE_NAME = "config.yaml"
PROFILE_FILE_SUFFIX = ".config.yaml"  # ~/.mote/<name>.config.yaml
MANAGED_CONFIG_FILE_NAME = "managed.config.yaml"  # /etc/mote/managed.config.yaml

_SYSTEM_CONFIG_DIR = Path("/etc/mote")
_WORKDIR_CONFIG_SUBDIR = ".mote"


class ConfigSource(IntEnum):
    """A config layer source. The integer value is the precedence (higher wins).

    Lower layers are overridden by higher ones during the merge. Gaps are left
    between values so future layers slot in without renumbering existing ones.
    """

    DEFAULT = 0  # pydantic field defaults (no file)
    SYSTEM = 10  # /etc/mote/config.yaml (+ config.d/*.yaml) — managed
    USER = 20  # ~/.mote/config.yaml
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


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Stable identity of one resolved config file within a discovery pass."""

    canonical_path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A concrete config file resolved to a particular source layer."""

    source: ConfigSource
    path: Path
    identity: SourceIdentity
    trusted: bool


def _existing(paths: Sequence[Optional[Path]]) -> List[Path]:
    return [p for p in paths if p is not None and p.is_file()]


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _descriptor(source: ConfigSource, path: Path, *, trusted_root: Path | None) -> SourceFile:
    canonical = path.resolve(strict=True)
    stat = canonical.stat()
    identity = SourceIdentity(canonical, stat.st_dev, stat.st_ino)
    trusted = False
    if source is not ConfigSource.WORKDIR and trusted_root is not None:
        canonical_root = trusted_root.resolve(strict=True)
        expected_owner = 0 if source in {ConfigSource.SYSTEM, ConfigSource.MANAGED} else os.getuid()
        trusted = _within(canonical, canonical_root) and stat.st_uid == expected_owner and stat.st_mode & 0o022 == 0
    return SourceFile(source=source, path=canonical, identity=identity, trusted=trusted)


def profile_path(profile: str, user_config_root: Path) -> Path:
    """The on-disk path of a named profile overlay (``~/.mote/<name>.config.yaml``)."""
    return user_config_root / f"{profile}{PROFILE_FILE_SUFFIX}"


def discover_source_files(
    cwd: Optional[Path] = None,
    *,
    profile: Optional[str] = None,
    user_config_root: Path | None = None,
) -> List[SourceFile]:
    """Resolve every config file that exists, in ascending precedence order.

    A single source may map to multiple files. They are listed low->high so a
    later file overrides an earlier one within the same source band. The trusted
    USER files are accepted only from their canonical Product-owned root.

    When ``profile`` is given, ``~/.mote/<profile>.config.yaml`` is added
    as a trusted PROFILE layer (above WORKDIR, below ENV) — a named overlay
    that selectively overrides the base config (mirrors codex profiles).
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    candidates: List[SourceFile] = []

    # SYSTEM: base file then drop-in dir (config.d/*.yaml, sorted alphabetically).
    for p in _existing([_SYSTEM_CONFIG_DIR / CONFIG_FILE_NAME]):
        candidates.append(_descriptor(ConfigSource.SYSTEM, p, trusted_root=_SYSTEM_CONFIG_DIR))
    sys_d = _SYSTEM_CONFIG_DIR / "config.d"
    if sys_d.is_dir():
        for p in sorted(sys_d.glob("*.yaml")):
            candidates.append(_descriptor(ConfigSource.SYSTEM, p, trusted_root=_SYSTEM_CONFIG_DIR))

    # USER: ~/.mote/config.yaml.
    user_files = [user_config_root / CONFIG_FILE_NAME] if user_config_root is not None else []
    for p in _existing(user_files):
        candidates.append(_descriptor(ConfigSource.USER, p, trusted_root=user_config_root))

    # WORKDIR (untrusted): <cwd>/.mote/config.yaml.
    for p in _existing([cwd / _WORKDIR_CONFIG_SUBDIR / CONFIG_FILE_NAME]):
        candidates.append(_descriptor(ConfigSource.WORKDIR, p, trusted_root=None))

    # PROFILE (trusted): named overlay ~/.mote/<profile>.config.yaml.
    if profile and user_config_root is not None:
        for p in _existing([profile_path(profile, user_config_root)]):
            candidates.append(_descriptor(ConfigSource.PROFILE, p, trusted_root=user_config_root))

    # MANAGED (trusted, highest): admin policy that overrides every other layer.
    for p in _existing([_SYSTEM_CONFIG_DIR / MANAGED_CONFIG_FILE_NAME]):
        candidates.append(_descriptor(ConfigSource.MANAGED, p, trusted_root=_SYSTEM_CONFIG_DIR))

    # A physical file has one effective descriptor.  Aliases always collapse to
    # the least trusted candidate; equal-trust aliases keep the lower precedence
    # so a caller-controlled path cannot promote an existing file.
    selected: dict[tuple[int, int], SourceFile] = {}
    for candidate in candidates:
        key = (candidate.identity.device, candidate.identity.inode)
        current = selected.get(key)
        rank = (candidate.trusted, int(candidate.source))
        if current is None or rank < (current.trusted, int(current.source)):
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: int(item.source))
