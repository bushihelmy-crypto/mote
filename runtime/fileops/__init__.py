"""Runtime implementation of Mote's File Operations bounded context."""

from mote.runtime.fileops.artifact_lifecycle import ArtifactLifecycleCatalog
from mote.runtime.fileops.byte_views import ByteViewService
from mote.runtime.fileops.capture import ManagedSnapshotCapture
from mote.runtime.fileops.checkpoints import WorktreeCheckpointStore
from mote.runtime.fileops.control import ProjectOperationControl
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.encoding import decode_text, editable_text
from mote.runtime.fileops.facade import FileOperations
from mote.runtime.fileops.identity import (
    name_identity,
    path_token,
    project_identity,
    resolve_existing_target,
    target_identity,
)
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import (
    ARTIFACT_LOCK_LEVEL,
    JOURNAL_LOCK_LEVEL,
    NAME_LOCK_LEVEL,
    PROJECT_LOCK_LEVEL,
    TARGET_LOCK_LEVEL,
    TIMELINE_LOCK_LEVEL,
    HierarchicalLockManager,
)
from mote.runtime.fileops.metadata import PreservedMetadata, capture_metadata
from mote.runtime.fileops.pdf_views import PdfViewService
from mote.runtime.fileops.publisher import AtomicPublisher
from mote.runtime.fileops.read_cursors import OpenReadCursor, ReadCursorStore
from mote.runtime.fileops.review import ReviewService
from mote.runtime.fileops.rewind import RewindCoordinator
from mote.runtime.fileops.snapshots import ObservedFileVersion, SealedSnapshotReader
from mote.runtime.fileops.text_sources import MaterializedText, TextSourceService
from mote.runtime.fileops.text_views import TextViewService
from mote.runtime.fileops.transactions import MutationCoordinator

__all__ = [
    "ARTIFACT_LOCK_LEVEL",
    "ArtifactLifecycleCatalog",
    "AtomicPublisher",
    "ByteViewService",
    "DurableFileOperationsJournal",
    "DurableCursorRegistry",
    "FileOperations",
    "HierarchicalLockManager",
    "JOURNAL_LOCK_LEVEL",
    "ManagedSnapshotCapture",
    "MaterializedText",
    "NAME_LOCK_LEVEL",
    "MutationCoordinator",
    "OpenReadCursor",
    "PROJECT_LOCK_LEVEL",
    "PreservedMetadata",
    "PdfViewService",
    "ProjectOperationControl",
    "ReviewService",
    "ReadCursorStore",
    "RewindCoordinator",
    "SealedSnapshotReader",
    "ObservedFileVersion",
    "TARGET_LOCK_LEVEL",
    "TIMELINE_LOCK_LEVEL",
    "TextViewService",
    "TextSourceService",
    "WorktreeCheckpointStore",
    "capture_metadata",
    "decode_text",
    "editable_text",
    "name_identity",
    "path_token",
    "project_identity",
    "resolve_existing_target",
    "target_identity",
]
