"""Typed failures emitted by the File Operations bounded context."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.errors.base import NonRetryableError, RetryableError
from mote.contracts.errors.codes import ErrorCode


class FileOperationError(NonRetryableError):
    pass


class StaleSnapshotError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_STALE_SNAPSHOT


class IdentityChangedError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_IDENTITY_CHANGED


class ContentChangedError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_CONTENT_CHANGED


class FileLockTimeoutError(RetryableError, FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_LOCK_TIMEOUT


class FileLockCancelledError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_LOCK_CANCELLED


class EncodingRejectedError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_ENCODING_REJECTED


class SnapshotDurabilityError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_SNAPSHOT_DURABILITY


class JournalDurabilityError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_JOURNAL_DURABILITY


class MetadataPreservationError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_METADATA_PRESERVATION


class FilePublishError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_PUBLISH


class UnsupportedFilesystemSemanticsError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_UNSUPPORTED_FILESYSTEM


class RecoveryInDoubtError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_RECOVERY_IN_DOUBT


class ReviewConflictError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_REVIEW_CONFLICT


class RewindFailedError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_REWIND_FAILED


class RecoveryFenceError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_RECOVERY_FENCE


class SearchPatternError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_SEARCH_PATTERN


class SearchDiscoveryError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_SEARCH_DISCOVERY


class SearchCursorError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_SEARCH_CURSOR


class FileReadRangeError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_READ_RANGE


class ReadCursorError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_READ_CURSOR


class PdfProcessingError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_PDF_PROCESSING


class DocumentExtractionError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_DOCUMENT_EXTRACTION


class DocumentExtractorUnavailableError(DocumentExtractionError):
    pass


class DocumentResourceLimitError(DocumentExtractionError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_DOCUMENT_RESOURCE_LIMIT


class FileBinaryContentError(FileOperationError):
    default_code: ClassVar[ErrorCode] = ErrorCode.FILE_BINARY_CONTENT


__all__ = [
    "ContentChangedError",
    "DocumentExtractionError",
    "DocumentExtractorUnavailableError",
    "DocumentResourceLimitError",
    "EncodingRejectedError",
    "FileLockCancelledError",
    "FileLockTimeoutError",
    "FileOperationError",
    "FileBinaryContentError",
    "FilePublishError",
    "FileReadRangeError",
    "IdentityChangedError",
    "JournalDurabilityError",
    "MetadataPreservationError",
    "PdfProcessingError",
    "RecoveryInDoubtError",
    "ReadCursorError",
    "RecoveryFenceError",
    "ReviewConflictError",
    "SearchCursorError",
    "SearchDiscoveryError",
    "SearchPatternError",
    "RewindFailedError",
    "SnapshotDurabilityError",
    "StaleSnapshotError",
    "UnsupportedFilesystemSemanticsError",
]
