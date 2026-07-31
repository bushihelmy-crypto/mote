"""File-domain observation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from mote.contracts.file.identity import FileChangeAttribution, FileChangeKind, FileVersion

if TYPE_CHECKING:
    pass

FILE_CHANGED = "file_changed"

FILE_MUTATED = "file_mutated"


@dataclass
class FileChangedEvent:
    """An exact, externally attributed file-version transition."""

    path: str
    change_type: FileChangeKind
    prior_version: FileVersion
    version: FileVersion
    attribution: FileChangeAttribution = FileChangeAttribution.EXTERNAL

    name: ClassVar[str] = FILE_CHANGED


@dataclass
class FileMutatedEvent:
    """A tool just successfully wrote/created/deleted a file on disk.

    Emitted by the :class:`ToolExecutor` right after a filesystem-mutating tool
    (Write/Edit/...) succeeds, carrying the resolved path. Purely
    an observation for derived services. It is not file-change attribution:
    only File Operations' exact durable commit facts establish a managed
    transition. Distinct from :class:`FileChangedEvent`, which the watcher emits
    for externally attributed transitions.
    """

    path: str = ""
    tool: str = ""
    operation: str = "update"  # create / update / delete (best-effort)

    name: ClassVar[str] = FILE_MUTATED
