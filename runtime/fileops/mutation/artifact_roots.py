"""Typed, fail-closed reachability projection for File Operations artifacts."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import (
    FileEditPlanStoredEvent,
    FileHistoryImportedEvent,
    FileOperationsEvent,
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
    HunkDetectedEvent,
    HunkReviewTransitionedEvent,
    RewindAbortedEvent,
    RewindCommittedEvent,
    RewindInDoubtEvent,
    RewindPreparedEvent,
)
from mote.contracts.file.codec import (
    blob_from_dict,
    search_skipped_from_dict,
    search_summary_from_dict,
    snapshot_from_dict,
)
from mote.contracts.file.errors import SnapshotDurabilityError
from mote.contracts.file.identity import AbsentVersion, FileSnapshot, PresentVersion
from mote.contracts.file.mutations import CreateMutation, DeleteMutation, ReplaceMutation
from mote.contracts.file.search import SearchOutputMode
from mote.contracts.file.transactions import HunkRecord, ReviewStatus, validate_committed_versions
from mote.contracts.file.views import ReadCursorKind, TextViewMode
from mote.runtime.fileops.edit_plans import AbsentEditPlanSource, EditPlanStore, ExistingEditPlanSource
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.mutation.artifacts import ArtifactRepository

_READ_MANIFEST_FORMAT = 1
_READ_MANIFEST_MAXIMUM = 64 * 1_024
_READ_MANIFEST_KEYS = frozenset({"format_version", "kind", "payload"})
_READ_PAYLOAD_KEYS = {
    ReadCursorKind.TEXT: frozenset({"snapshot", "text_artifact", "mode"}),
    ReadCursorKind.RAW: frozenset({"snapshot"}),
    ReadCursorKind.HEX: frozenset({"snapshot"}),
    ReadCursorKind.PDF_TEXT: frozenset({"snapshot", "dpi", "adapter"}),
    ReadCursorKind.PDF_RENDER: frozenset({"snapshot", "dpi", "adapter"}),
}

_SEARCH_RESULT_FORMAT = 2
_SEARCH_RESULT_MAXIMUM = 1_024 * 1_024
_SEARCH_RESULT_KEYS = frozenset(
    {
        "format_version",
        "rows_artifact",
        "row_count",
        "skipped_artifact",
        "summary",
        "skipped_preview",
        "output_mode",
        "content_search",
    }
)


class ArtifactReachabilityError(SnapshotDurabilityError):
    """The authoritative roots or a typed manifest cannot be projected."""


class ArtifactRootKind(StrEnum):
    LEAF = "leaf"
    EDIT_PLAN_MANIFEST = "edit_plan_manifest"
    READ_MANIFEST = "read_manifest"
    SEARCH_RESULT_MANIFEST = "search_result_manifest"


@dataclass(frozen=True, slots=True)
class ArtifactRoot:
    artifact: ContentIdentity
    kind: ArtifactRootKind
    identity: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactReachability:
    roots: tuple[ArtifactRoot, ...]
    artifacts: tuple[ContentIdentity, ...]


class ExternalArtifactRootSource(Protocol):
    """A durable authority for additional objects stored in the shared CAS."""

    def scan_blob_roots(self) -> tuple[ContentIdentity, ...]: ...


@dataclass
class _TransactionProjection:
    prepared: FileTransactionPreparedEvent
    terminal: str = ""


class ArtifactReachabilityProjector:
    """Projects permanent journal roots and closes typed artifact manifests."""

    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        edit_plans: EditPlanStore,
        journal: DurableFileOperationsJournal,
    ) -> None:
        if type(repository) is not ArtifactRepository:
            raise TypeError("artifact reachability requires an ArtifactRepository")
        self.repository = repository
        self.edit_plans = edit_plans
        self.journal = journal
        self._external_sources: list[ExternalArtifactRootSource] = []

    def register_root_source(self, source: ExternalArtifactRootSource) -> None:
        if not callable(getattr(source, "scan_blob_roots", None)):
            raise TypeError("external artifact root source must be scannable")
        if source not in self._external_sources:
            self._external_sources.append(source)

    def scan(self) -> ArtifactReachability:
        projected = self.project_events(self.journal.iter_events())
        external_roots = tuple(
            ArtifactRoot(ref, ArtifactRootKind.LEAF, type(source).__name__)
            for source in tuple(self._external_sources)
            for ref in source.scan_blob_roots()
        )
        roots = self._canonical_roots((*projected.roots, *external_roots))
        return ArtifactReachability(roots=roots, artifacts=self.close(roots))

    def project_events(
        self,
        events: Iterable[FileOperationsEvent],
    ) -> ArtifactReachability:
        roots = self.event_roots(events)
        return ArtifactReachability(roots=roots, artifacts=self.close(roots))

    def event_roots(
        self,
        events: Iterable[FileOperationsEvent],
    ) -> tuple[ArtifactRoot, ...]:
        transactions: dict[str, _TransactionProjection] = {}
        edit_plans: dict[str, ContentIdentity] = {}
        reviews: dict[str, HunkRecord] = {}
        prepared_reviews: dict[str, tuple[HunkRecord, ...]] = {}
        imported_history: dict[str, FileHistoryImportedEvent] = {}

        for event in events:
            if isinstance(event, FileHistoryImportedEvent):
                prior = imported_history.get(event.import_id)
                if prior is not None and prior != event:
                    raise ArtifactReachabilityError(
                        "imported file history id resolves to conflicting facts",
                        import_id=event.import_id,
                    )
                imported_history[event.import_id] = event
                continue
            if isinstance(event, FileTransactionPreparedEvent):
                transaction_id = event.mutation_set.transaction_id
                if transaction_id in transactions:
                    raise ArtifactReachabilityError(
                        "file transaction is prepared more than once",
                        transaction_id=transaction_id,
                    )
                transactions[transaction_id] = _TransactionProjection(event)
                prepared_reviews[transaction_id] = event.hunks
                continue
            if isinstance(
                event,
                (
                    FileTransactionCommittedEvent,
                    FileTransactionAbortedEvent,
                    FileTransactionInDoubtEvent,
                ),
            ):
                projection = transactions.get(event.transaction_id)
                if projection is None or projection.terminal:
                    raise ArtifactReachabilityError(
                        "file transaction terminal event has no unique preparation",
                        transaction_id=event.transaction_id,
                    )
                if isinstance(event, FileTransactionCommittedEvent):
                    try:
                        validate_committed_versions(
                            projection.prepared.mutation_set,
                            event.versions,
                        )
                    except (TypeError, ValueError) as exc:
                        raise ArtifactReachabilityError(
                            "committed versions do not match their mutation set",
                            transaction_id=event.transaction_id,
                            cause=exc,
                        ) from exc
                    self._validate_committed_content(
                        projection.prepared,
                        event,
                    )
                    projection.terminal = "committed"
                    for record in prepared_reviews.pop(event.transaction_id, ()):
                        prior = reviews.get(record.hunk_id)
                        if prior is not None and prior != record:
                            raise ArtifactReachabilityError(
                                "committed hunk conflicts with an existing review",
                                hunk_id=record.hunk_id,
                            )
                        reviews.setdefault(record.hunk_id, record)
                elif isinstance(event, FileTransactionAbortedEvent):
                    projection.terminal = "aborted"
                    prepared_reviews.pop(event.transaction_id, None)
                else:
                    projection.terminal = "in_doubt"
                    prepared_reviews.pop(event.transaction_id, None)
                continue
            if isinstance(event, FileEditPlanStoredEvent):
                prior = edit_plans.get(event.plan_id)
                if prior is not None and prior != event.manifest:
                    raise ArtifactReachabilityError(
                        "edit plan id resolves to conflicting manifests",
                        plan_id=event.plan_id,
                    )
                edit_plans[event.plan_id] = event.manifest
                continue
            if isinstance(event, HunkDetectedEvent):
                if event.record.version != 1 or event.record.status != ReviewStatus.PENDING:
                    raise ArtifactReachabilityError(
                        "detected hunk does not begin at the pending version",
                        hunk_id=event.record.hunk_id,
                    )
                prior = reviews.get(event.record.hunk_id)
                if prior is not None:
                    if prior != event.record:
                        raise ArtifactReachabilityError(
                            "hunk id resolves to conflicting records",
                            hunk_id=event.record.hunk_id,
                        )
                    continue
                reviews[event.record.hunk_id] = event.record
                continue
            if isinstance(event, HunkReviewTransitionedEvent):
                prior = reviews.get(event.hunk_id)
                if (
                    prior is None
                    or prior.version != event.expected_version
                    or event.version != event.expected_version + 1
                    or not isinstance(event.status, ReviewStatus)
                ):
                    raise ArtifactReachabilityError(
                        "hunk transition does not follow the durable review version",
                        hunk_id=event.hunk_id,
                    )
                reviews[event.hunk_id] = replace(
                    prior,
                    status=event.status,
                    new_range=event.new_range,
                    post_hash=event.post_hash,
                    expected_digest=event.expected_digest,
                    child_transaction_id=event.child_transaction_id,
                    version=event.version,
                )
                continue
            if isinstance(
                event,
                (
                    RewindPreparedEvent,
                    RewindCommittedEvent,
                    RewindAbortedEvent,
                    RewindInDoubtEvent,
                ),
            ):
                continue
            raise ArtifactReachabilityError(
                "unknown File Operations event cannot be projected",
                event_type=type(event).__name__,
            )

        roots: list[ArtifactRoot] = []
        for import_id in sorted(imported_history):
            before = imported_history[import_id].before
            if before is not None:
                roots.append(ArtifactRoot(before, ArtifactRootKind.LEAF, import_id))
        for transaction_id in sorted(transactions):
            projection = transactions[transaction_id]
            if projection.terminal == "aborted":
                continue
            roots.extend(self._mutation_roots(projection.prepared))
            if projection.terminal in ("", "in_doubt"):
                for record in projection.prepared.hunks:
                    roots.extend(self._hunk_roots(record))
        for plan_id in sorted(edit_plans):
            roots.append(
                ArtifactRoot(
                    edit_plans[plan_id],
                    ArtifactRootKind.EDIT_PLAN_MANIFEST,
                    plan_id,
                )
            )
        for hunk_id in sorted(reviews):
            roots.extend(self._hunk_roots(reviews[hunk_id]))
        return self._canonical_roots(roots)

    def close(self, roots: Iterable[ArtifactRoot]) -> tuple[ContentIdentity, ...]:
        closed: dict[str, ContentIdentity] = {}
        for root in self._canonical_roots(roots):
            self._add(closed, root.artifact)
            if root.kind == ArtifactRootKind.LEAF:
                continue
            elif root.kind == ArtifactRootKind.EDIT_PLAN_MANIFEST:
                self._expand_edit_plan(root.artifact, root.identity, closed)
            elif root.kind == ArtifactRootKind.READ_MANIFEST:
                self._expand_read_manifest(root.artifact, closed)
            elif root.kind == ArtifactRootKind.SEARCH_RESULT_MANIFEST:
                self._expand_search_manifest(root.artifact, closed)
            else:
                raise ArtifactReachabilityError(
                    "unknown artifact root kind cannot be expanded",
                    kind=str(root.kind),
                )
        for artifact in closed.values():
            try:
                self.repository.read_bounded(
                    artifact,
                    maximum_bytes=artifact.size,
                )
            except Exception as exc:
                raise ArtifactReachabilityError(
                    "reachable artifact is missing or corrupt",
                    digest=artifact.digest,
                    cause=exc,
                ) from exc
        return tuple(sorted(closed.values(), key=lambda item: (item.digest, item.size)))

    def read_manifest_root(self, manifest: ContentIdentity) -> ArtifactRoot:
        return ArtifactRoot(manifest, ArtifactRootKind.READ_MANIFEST)

    def search_result_root(self, manifest: ContentIdentity) -> ArtifactRoot:
        return ArtifactRoot(manifest, ArtifactRootKind.SEARCH_RESULT_MANIFEST)

    def _expand_edit_plan(
        self,
        manifest: ContentIdentity,
        expected_plan_id: str,
        closed: dict[str, ContentIdentity],
    ) -> None:
        try:
            plan = self.edit_plans.load_manifest(manifest)
        except Exception as exc:
            if isinstance(exc, ArtifactReachabilityError):
                raise
            raise ArtifactReachabilityError(
                "edit plan manifest closure is invalid",
                digest=manifest.digest,
                cause=exc,
            ) from exc
        if plan.plan_id != expected_plan_id:
            raise ArtifactReachabilityError(
                "edit plan event identity does not match its manifest",
                expected_plan_id=expected_plan_id,
                actual_plan_id=plan.plan_id,
            )
        self._add(closed, plan.request_artifact)
        for source in plan.sources:
            if isinstance(source, ExistingEditPlanSource):
                self._add_snapshot(closed, source.snapshot)
            elif isinstance(source, AbsentEditPlanSource):
                self._add(closed, source.metadata)
            else:
                raise ArtifactReachabilityError("edit plan contains an unknown source kind")
        for fact in plan.review_facts:
            self._add(closed, fact.before_utf8)
            self._add(closed, fact.after_utf8)
        for mutation in plan.mutation_set.mutations:
            self._add_mutation(closed, mutation)

    def _expand_read_manifest(
        self,
        manifest: ContentIdentity,
        closed: dict[str, ContentIdentity],
    ) -> None:
        payload = self._load_object(
            manifest,
            maximum=_READ_MANIFEST_MAXIMUM,
            label="read cursor manifest",
        )
        if set(payload) != _READ_MANIFEST_KEYS:
            raise ArtifactReachabilityError("read cursor manifest fields are not canonical")
        if type(payload["format_version"]) is not int:
            raise ArtifactReachabilityError("read cursor manifest format is not an integer")
        if payload["format_version"] != _READ_MANIFEST_FORMAT:
            raise ArtifactReachabilityError("unsupported read cursor manifest format")
        if type(payload["kind"]) is not str:
            raise ArtifactReachabilityError("read cursor kind is not a string")
        try:
            kind = ReadCursorKind(payload["kind"])
        except ValueError as exc:
            raise ArtifactReachabilityError("read cursor kind is invalid") from exc
        values = payload["payload"]
        if type(values) is not dict or set(values) != _READ_PAYLOAD_KEYS[kind]:
            raise ArtifactReachabilityError("read cursor payload fields are not canonical")
        try:
            snapshot = snapshot_from_dict(values["snapshot"])
            self._add_snapshot(closed, snapshot)
            if kind == ReadCursorKind.TEXT:
                text_artifact = blob_from_dict(values["text_artifact"])
                if text_artifact is None:
                    raise ValueError("text artifact is missing")
                mode = TextViewMode(values["mode"])
                if mode == TextViewMode.TEXT and snapshot.encoding is None:
                    raise ValueError("text cursor snapshot has no encoding")
                if mode == TextViewMode.DOCUMENT and snapshot.encoding is not None:
                    raise ValueError("document cursor snapshot has an encoding")
                self._add(closed, text_artifact)
            elif kind in (ReadCursorKind.PDF_TEXT, ReadCursorKind.PDF_RENDER):
                dpi = values["dpi"]
                adapter = values["adapter"]
                if type(dpi) is not int or dpi <= 0:
                    raise ValueError("PDF cursor DPI is invalid")
                if type(adapter) is not str or not adapter:
                    raise ValueError("PDF cursor adapter is invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactReachabilityError(
                "read cursor manifest payload is invalid",
                cause=exc,
            ) from exc

    def _expand_search_manifest(
        self,
        manifest: ContentIdentity,
        closed: dict[str, ContentIdentity],
    ) -> None:
        payload = self._load_object(
            manifest,
            maximum=_SEARCH_RESULT_MAXIMUM,
            label="search result manifest",
        )
        if set(payload) != _SEARCH_RESULT_KEYS:
            raise ArtifactReachabilityError("search result manifest fields are not canonical")
        try:
            if type(payload["format_version"]) is not int:
                raise TypeError("search result format is not an integer")
            if payload["format_version"] != _SEARCH_RESULT_FORMAT:
                raise ValueError("unsupported search result format")
            rows = blob_from_dict(payload["rows_artifact"])
            skipped = blob_from_dict(payload["skipped_artifact"])
            if rows is None or skipped is None:
                raise ValueError("search result child artifact is missing")
            if type(payload["row_count"]) is not int or payload["row_count"] < 0:
                raise TypeError("search row count is invalid")
            search_summary_from_dict(payload["summary"])
            if type(payload["skipped_preview"]) is not list:
                raise TypeError("search skipped preview is not a list")
            for item in payload["skipped_preview"]:
                search_skipped_from_dict(item)
            if type(payload["output_mode"]) is not str:
                raise TypeError("search output mode is not a string")
            SearchOutputMode(payload["output_mode"])
            if type(payload["content_search"]) is not bool:
                raise TypeError("search content flag is not a boolean")
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactReachabilityError(
                "search result manifest payload is invalid",
                cause=exc,
            ) from exc
        self._add(closed, rows)
        self._add(closed, skipped)

    def _mutation_roots(
        self,
        event: FileTransactionPreparedEvent,
    ) -> list[ArtifactRoot]:
        closed: dict[str, ContentIdentity] = {}
        for mutation in event.mutation_set.mutations:
            self._add_mutation(closed, mutation)
        return [ArtifactRoot(ref, ArtifactRootKind.LEAF) for ref in closed.values()]

    @staticmethod
    def _validate_committed_content(
        prepared: FileTransactionPreparedEvent,
        committed: FileTransactionCommittedEvent,
    ) -> None:
        expected_paths = tuple(mutation.target_path.display for mutation in prepared.mutation_set.mutations)
        if committed.paths != expected_paths:
            raise ArtifactReachabilityError(
                "committed paths do not match the prepared mutations",
                transaction_id=committed.transaction_id,
            )
        for mutation, version in zip(
            prepared.mutation_set.mutations,
            committed.versions,
            strict=True,
        ):
            if isinstance(mutation, DeleteMutation):
                if not isinstance(version, AbsentVersion):
                    raise ArtifactReachabilityError(
                        "delete transaction committed a present version",
                        transaction_id=committed.transaction_id,
                    )
                continue
            if not isinstance(version, PresentVersion):
                raise ArtifactReachabilityError(
                    "write transaction committed an absent version",
                    transaction_id=committed.transaction_id,
                )
            expected_metadata = mutation.metadata if isinstance(mutation, CreateMutation) else mutation.before.metadata
            if (
                version.digest != mutation.after.digest
                or version.size != mutation.after.size
                or version.metadata_digest != expected_metadata.digest
            ):
                raise ArtifactReachabilityError(
                    "committed version content does not match its mutation artifacts",
                    transaction_id=committed.transaction_id,
                )

    def _hunk_roots(self, record: HunkRecord) -> list[ArtifactRoot]:
        roots: list[ArtifactRoot] = []
        for digest in (record.pre_hash, record.post_hash, record.expected_digest):
            try:
                ref = self.repository.resolve_live(digest)
            except Exception as exc:
                raise ArtifactReachabilityError(
                    "hunk references a missing or corrupt artifact",
                    hunk_id=record.hunk_id,
                    digest=digest,
                    cause=exc,
                ) from exc
            roots.append(ArtifactRoot(ref, ArtifactRootKind.LEAF))
        return roots

    @staticmethod
    def _add_mutation(closed: dict[str, ContentIdentity], mutation: Any) -> None:
        if isinstance(mutation, CreateMutation):
            ArtifactReachabilityProjector._add(closed, mutation.after)
            ArtifactReachabilityProjector._add(closed, mutation.metadata)
        elif isinstance(mutation, ReplaceMutation):
            ArtifactReachabilityProjector._add_snapshot(closed, mutation.before)
            ArtifactReachabilityProjector._add(closed, mutation.after)
        elif isinstance(mutation, DeleteMutation):
            ArtifactReachabilityProjector._add_snapshot(closed, mutation.before)
        else:
            raise ArtifactReachabilityError("transaction contains an unknown mutation kind")

    @staticmethod
    def _add_snapshot(closed: dict[str, ContentIdentity], snapshot: FileSnapshot) -> None:
        if not isinstance(snapshot, FileSnapshot):
            raise ArtifactReachabilityError("file snapshot has an invalid type")
        ArtifactReachabilityProjector._add(closed, snapshot.artifact)
        ArtifactReachabilityProjector._add(closed, snapshot.metadata)

    @staticmethod
    def _add(closed: dict[str, ContentIdentity], artifact: ContentIdentity) -> None:
        if not isinstance(artifact, ContentIdentity):
            raise ArtifactReachabilityError("artifact reference has an invalid type")
        prior = closed.get(artifact.digest)
        if prior is not None and prior.size != artifact.size:
            raise ArtifactReachabilityError(
                "artifact digest resolves to conflicting sizes",
                digest=artifact.digest,
                first_size=prior.size,
                second_size=artifact.size,
            )
        closed[artifact.digest] = artifact

    @staticmethod
    def _canonical_roots(roots: Iterable[ArtifactRoot]) -> tuple[ArtifactRoot, ...]:
        canonical: dict[tuple[ArtifactRootKind, str, str], ArtifactRoot] = {}
        sizes: dict[str, int] = {}
        for root in roots:
            if not isinstance(root, ArtifactRoot):
                raise ArtifactReachabilityError("artifact root has an invalid type")
            prior_size = sizes.get(root.artifact.digest)
            if prior_size is not None and prior_size != root.artifact.size:
                raise ArtifactReachabilityError(
                    "artifact root digest resolves to conflicting sizes",
                    digest=root.artifact.digest,
                )
            sizes[root.artifact.digest] = root.artifact.size
            canonical[(root.kind, root.artifact.digest, root.identity)] = root
        return tuple(
            sorted(
                canonical.values(),
                key=lambda item: (
                    item.kind.value,
                    item.artifact.digest,
                    item.identity,
                ),
            )
        )

    def _load_object(
        self,
        artifact: ContentIdentity,
        *,
        maximum: int,
        label: str,
    ) -> dict[str, Any]:
        try:
            raw = self.repository.read_bounded(
                artifact,
                maximum_bytes=maximum,
            )
            payload = json.loads(raw.decode("ascii", errors="strict"))
            if type(payload) is not dict:
                raise TypeError(f"{label} is not an object")
            return payload
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactReachabilityError(
                f"{label} is invalid",
                digest=artifact.digest,
                cause=exc,
            ) from exc
        except SnapshotDurabilityError as exc:
            raise ArtifactReachabilityError(
                f"{label} is missing or corrupt",
                digest=artifact.digest,
                cause=exc,
            ) from exc


__all__ = [
    "ArtifactReachability",
    "ArtifactReachabilityError",
    "ArtifactReachabilityProjector",
    "ArtifactRoot",
    "ArtifactRootKind",
]
