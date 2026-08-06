"""Immutable, durable project edit plans over sealed text artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, ClassVar

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.codec import (
    blob_from_dict,
    blob_to_dict,
    encoding_from_dict,
    encoding_to_dict,
    mutation_set_from_dict,
    mutation_set_to_dict,
    mutation_to_dict,
    path_from_dict,
    path_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
    version_from_dict,
    version_to_dict,
)
from mote.contracts.file.errors import EncodingRejectedError, StaleSnapshotError
from mote.contracts.file.identity import (
    AbsentVersion,
    EncodingDecision,
    FileSnapshot,
    NewlineProfile,
    PathToken,
    ProjectIdentity,
)
from mote.contracts.file.mutations import CreateMutation, DeleteMutation, MutationSet, ReplaceMutation
from mote.contracts.file.views import TextViewMode
from mote.runtime.fileops.candidate_discovery import CandidateDiscoveryService
from mote.runtime.fileops.encoding import decode_text, editable_text
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.metadata_manifest import MAX_METADATA_MANIFEST_BYTES
from mote.runtime.fileops.mutation.artifacts import ArtifactWriteScope, FileMutationArtifactRepository
from mote.runtime.fileops.mutation_factory import MutationFactory
from mote.runtime.fileops.query_semantics import (
    CandidateDiscovery,
    CandidateDiscoveryRequest,
    RegexProgram,
    RegexProgramError,
)
from mote.runtime.fileops.reservation_owners import artifact_owner
from mote.runtime.fileops.resource_limits import (
    ARTIFACT_WRITE_TTL_SECONDS,
    MAX_EDIT_PLAN_ARTIFACT_BYTES,
    MAX_EDIT_PLAN_REVIEW_FACT_BYTES,
)
from mote.runtime.fileops.text_sources import TextSourceService

MAX_EDIT_PLAN_MANIFEST_BYTES = 4 * 1_024 * 1_024
MAX_EDIT_PLAN_OUTPUT_BYTES = 10 * 1_024 * 1_024
MAX_EDIT_PLAN_REQUEST_BYTES = 64 * 1_024 * 1_024
_FORMAT_VERSION = 2
_MANIFEST_KEYS = frozenset(
    {
        "format_version",
        "plan_id",
        "transaction_id",
        "request",
        "discovery",
        "sources",
        "preview",
        "review_facts",
        "mutation_set",
    }
)


class EditPlanManifestError(ValueError):
    """A durable edit plan manifest is invalid or exceeds its boundary."""


class EditPlanSourceError(ValueError):
    """A discovered source cannot participate in a text edit plan."""


class ReplacementLimitExceededError(ValueError):
    def __init__(self, maximum: int, actual: int) -> None:
        super().__init__(f"edit plan has {actual} replacements, exceeding the limit {maximum}")
        self.maximum = maximum
        self.actual = actual


class EditPlanOutputLimitError(ValueError):
    def __init__(self, maximum: int, actual: int) -> None:
        super().__init__(f"edit plan output has {actual} bytes, exceeding the limit {maximum}")
        self.maximum = maximum
        self.actual = actual


class EditPlanRequestKind(StrEnum):
    REGEX = "regex"
    LITERAL = "literal"
    WHOLE_FILE = "whole_file"


class EditSourceKind(StrEnum):
    EXISTING_PLAN = "existing_plan"
    ABSENT_PLAN = "absent_plan"


@dataclass(frozen=True, slots=True)
class RegexEditPlanRequest:
    root: PathToken
    pattern: str
    replacement: str
    globs: tuple[str, ...] = ()
    type_name: str = ""
    case_insensitive: bool = False
    multiline: bool = False
    encoding: str | None = None
    fallback_encoding: str | None = None
    max_replacements: int = 1_000
    timeout: float = 20.0

    kind: ClassVar[EditPlanRequestKind] = EditPlanRequestKind.REGEX

    def __post_init__(self) -> None:
        discovery = CandidateDiscoveryRequest(
            root=self.root,
            globs=self.globs,
            type_name=self.type_name,
        )
        object.__setattr__(self, "globs", discovery.globs)
        for field, value in (
            ("pattern", self.pattern),
            ("replacement", self.replacement),
        ):
            if type(value) is not str:
                raise TypeError(f"edit plan {field} must be a string")
        for field, value in (
            ("case_insensitive", self.case_insensitive),
            ("multiline", self.multiline),
        ):
            if type(value) is not bool:
                raise TypeError(f"edit plan {field} must be a boolean")
        for field, value in (
            ("encoding", self.encoding),
            ("fallback_encoding", self.fallback_encoding),
        ):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"edit plan {field} is invalid")
        if type(self.max_replacements) is not int or self.max_replacements <= 0:
            raise ValueError("edit plan max_replacements must be positive")
        if type(self.timeout) not in (int, float) or isinstance(self.timeout, bool) or self.timeout <= 0:
            raise ValueError("edit plan timeout must be positive")


@dataclass(frozen=True, slots=True)
class LiteralEditPlanRequest:
    path: PathToken
    old: str
    new: str
    replace_all: bool = False

    kind: ClassVar[EditPlanRequestKind] = EditPlanRequestKind.LITERAL

    def __post_init__(self) -> None:
        CandidateDiscoveryRequest(root=self.path)
        if type(self.old) is not str or not self.old:
            raise ValueError("literal edit old text must be non-empty")
        if type(self.new) is not str:
            raise TypeError("literal edit new text must be a string")
        if type(self.replace_all) is not bool:
            raise TypeError("literal edit replace_all must be a boolean")


@dataclass(frozen=True, slots=True)
class WholeFileEditPlanRequest:
    path: PathToken
    content: str
    encoding: str | None = None

    kind: ClassVar[EditPlanRequestKind] = EditPlanRequestKind.WHOLE_FILE

    def __post_init__(self) -> None:
        CandidateDiscoveryRequest(root=self.path)
        if type(self.content) is not str:
            raise TypeError("whole-file edit content must be a string")
        if self.encoding is not None and (type(self.encoding) is not str or not self.encoding):
            raise ValueError("whole-file edit encoding is invalid")


EditPlanRequest = RegexEditPlanRequest | LiteralEditPlanRequest | WholeFileEditPlanRequest


@dataclass(frozen=True, slots=True)
class ExistingEditPlanSource:
    snapshot: FileSnapshot
    newline_profile: NewlineProfile
    replacement_count: int

    kind: ClassVar[EditSourceKind] = EditSourceKind.EXISTING_PLAN

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, FileSnapshot):
            raise TypeError("existing edit source snapshot is invalid")
        if not isinstance(self.newline_profile, NewlineProfile):
            raise TypeError("existing edit source newline profile is invalid")
        if type(self.replacement_count) is not int or self.replacement_count <= 0:
            raise ValueError("existing edit source replacement count is invalid")


@dataclass(frozen=True, slots=True)
class AbsentEditPlanSource:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    expected_version: AbsentVersion
    metadata: ContentIdentity
    encoding: EncodingDecision
    newline_profile: NewlineProfile
    replacement_count: int

    kind: ClassVar[EditSourceKind] = EditSourceKind.ABSENT_PLAN

    def __post_init__(self) -> None:
        if not isinstance(self.expected_version, AbsentVersion):
            raise TypeError("absent edit source version is invalid")
        if not isinstance(self.encoding, EncodingDecision):
            raise TypeError("absent edit source encoding is invalid")
        if not isinstance(self.newline_profile, NewlineProfile):
            raise TypeError("absent edit source newline profile is invalid")
        if type(self.replacement_count) is not int or self.replacement_count <= 0:
            raise ValueError("absent edit source replacement count is invalid")


EditPlanSource = ExistingEditPlanSource | AbsentEditPlanSource


@dataclass(frozen=True, slots=True)
class EditPlanFilePreview:
    path: PathToken
    replacement_count: int


@dataclass(frozen=True, slots=True)
class EditPlanPreview:
    total_replacements: int
    affected_files: tuple[EditPlanFilePreview, ...]


@dataclass(frozen=True, slots=True)
class EditPlanReviewFact:
    path: PathToken
    before_utf8: ContentIdentity
    after_utf8: ContentIdentity


@dataclass(frozen=True, slots=True)
class EditPlan:
    plan_id: str
    transaction_id: str
    manifest_artifact: ContentIdentity
    request_artifact: ContentIdentity
    request: EditPlanRequest
    discovery: CandidateDiscovery
    sources: tuple[EditPlanSource, ...]
    preview: EditPlanPreview
    review_facts: tuple[EditPlanReviewFact, ...]
    mutation_set: MutationSet


class EditPlanStore:
    """Persists canonical manifests and journals their only durable reachability."""

    def __init__(
        self,
        *,
        artifacts: FileMutationArtifactRepository,
        journal: DurableFileOperationsJournal,
        session_id: str,
    ) -> None:
        self.artifacts = artifacts
        self.journal = journal
        self.session_id = session_id

    def persist(
        self,
        *,
        plan_id: str,
        transaction_id: str,
        request: EditPlanRequest,
        discovery: CandidateDiscovery,
        sources: tuple[EditPlanSource, ...],
        preview: EditPlanPreview,
        review_facts: tuple[EditPlanReviewFact, ...],
        mutation_set: MutationSet,
        scope: ArtifactWriteScope,
    ) -> EditPlan:
        request_raw = _canonical_json(_request_to_dict(request))
        if len(request_raw) > MAX_EDIT_PLAN_REQUEST_BYTES:
            raise EditPlanManifestError("edit plan request exceeds the size limit")
        request_artifact = scope.put_bytes(request_raw)
        payload = _plan_payload(
            plan_id=plan_id,
            transaction_id=transaction_id,
            request_artifact=request_artifact,
            discovery=discovery,
            sources=sources,
            preview=preview,
            review_facts=review_facts,
            mutation_set=mutation_set,
        )
        raw = _canonical_json(payload)
        if len(raw) > MAX_EDIT_PLAN_MANIFEST_BYTES:
            raise EditPlanManifestError("edit plan manifest exceeds the size limit")
        manifest = scope.put_bytes(raw)
        plan = self.load_manifest(manifest)
        self.journal.publish_edit_plan(plan_id, manifest)
        scope.complete(durability_root=self.journal.path.parent)
        return plan

    def load(self, plan_id: str) -> EditPlan:
        if re.fullmatch(r"[0-9a-f]{64}", plan_id) is None:
            raise EditPlanManifestError("edit plan id is invalid")
        manifest = self.journal.edit_plan_manifest(plan_id)
        if manifest is None:
            raise EditPlanManifestError("edit plan is not durably reachable")
        plan = self.load_manifest(manifest)
        if plan.plan_id != plan_id:
            raise EditPlanManifestError("edit plan id does not match its manifest")
        return plan

    def load_manifest(self, manifest: ContentIdentity) -> EditPlan:
        if manifest.size > MAX_EDIT_PLAN_MANIFEST_BYTES:
            raise EditPlanManifestError("edit plan manifest exceeds the size limit")
        try:
            raw = self.artifacts.read_bounded(
                manifest,
                maximum_bytes=MAX_EDIT_PLAN_MANIFEST_BYTES,
            )
            payload = json.loads(raw.decode("ascii", errors="strict"))
            plan = _plan_from_payload(payload, manifest, self.artifacts)
            _validate_plan_identity(plan)
            _validate_plan_artifacts(plan, self.artifacts)
            if plan.mutation_set.session_id != self.session_id:
                raise ValueError("edit plan belongs to another session")
            return plan
        except EditPlanManifestError:
            raise
        except (KeyError, TypeError, UnicodeError, ValueError) as exc:
            raise EditPlanManifestError("edit plan manifest is invalid") from exc


class EditPlanner:
    """Builds B1 bytes once from sealed editable text and persists one plan."""

    def __init__(
        self,
        *,
        artifacts: FileMutationArtifactRepository,
        sources: TextSourceService,
        discovery: CandidateDiscoveryService,
        store: EditPlanStore,
        resolve_observed: Callable[[PathToken], FileSnapshot | None],
        mutation_factory: MutationFactory,
    ) -> None:
        self.artifacts = artifacts
        self.sources = sources
        self.discovery = discovery
        self.store = store
        self.resolve_observed = resolve_observed
        self.mutation_factory = mutation_factory

    def plan(self, request: EditPlanRequest, *, transaction_id: str | None = None) -> EditPlan:
        regex_preparation = None
        if isinstance(request, RegexEditPlanRequest):
            regex_preparation = self._prepare_regex(request)
        elif not isinstance(
            request,
            (LiteralEditPlanRequest, WholeFileEditPlanRequest),
        ):
            raise TypeError("edit plan request is invalid")
        with self.artifacts.write_scope(
            owner=artifact_owner("edit-plan", self.store.session_id),
            maximum_bytes=MAX_EDIT_PLAN_ARTIFACT_BYTES,
            ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
        ) as scope:
            if isinstance(request, RegexEditPlanRequest):
                if regex_preparation is None:
                    raise RuntimeError("regex preparation is unavailable")
                program, discovery = regex_preparation
                return self._plan_regex(
                    request,
                    scope,
                    program=program,
                    discovery=discovery,
                    transaction_id=transaction_id,
                )
            if isinstance(request, LiteralEditPlanRequest):
                return self._plan_literal(request, scope, transaction_id=transaction_id)
            return self._plan_whole_file(request, scope, transaction_id=transaction_id)

    def _prepare_regex(
        self,
        request: RegexEditPlanRequest,
    ) -> tuple[RegexProgram, CandidateDiscovery]:
        try:
            program = RegexProgram.for_edit(
                request.pattern,
                case_insensitive=request.case_insensitive,
                dot_matches_newline=request.multiline,
            )
        except RegexProgramError as exc:
            raise EditPlanSourceError(str(exc)) from exc
        discovery = self.discovery.discover(
            CandidateDiscoveryRequest(
                root=request.root,
                globs=request.globs,
                type_name=request.type_name,
            ),
            timeout=request.timeout,
        )
        return program, discovery

    def _plan_regex(
        self,
        request: RegexEditPlanRequest,
        scope: ArtifactWriteScope,
        *,
        program: RegexProgram,
        discovery: CandidateDiscovery,
        transaction_id: str | None,
    ) -> EditPlan:
        planned: list[tuple[ExistingEditPlanSource, bytes]] = []
        total = 0
        for candidate in discovery.candidates:
            self.artifacts.renew(
                scope.reservation,
                ARTIFACT_WRITE_TTL_SECONDS,
            )
            materialized = self.sources.materialize(
                candidate,
                scope=scope,
                encoding=request.encoding,
                fallback_encoding=request.fallback_encoding,
            )
            if materialized.mode != TextViewMode.TEXT:
                raise EditPlanSourceError(f"document extracted text is not editable: {candidate.display}")
            snapshot = materialized.snapshot
            if snapshot.encoding is None:
                raise EditPlanSourceError(f"text source has no sealed encoding: {candidate.display}")
            raw = self.artifacts.read_bounded(
                snapshot.artifact,
                maximum_bytes=snapshot.version.size,
            )
            editable = editable_text(raw, snapshot.encoding)
            matches = tuple(program.finditer(editable.text))
            if not matches:
                continue
            total += len(matches)
            planned.append(
                (
                    ExistingEditPlanSource(
                        snapshot=snapshot,
                        newline_profile=editable.newline_profile,
                        replacement_count=len(matches),
                    ),
                    _replace_raw(raw, editable, program, matches, request.replacement),
                )
            )
        if total > request.max_replacements:
            raise ReplacementLimitExceededError(request.max_replacements, total)
        if not planned:
            raise EditPlanSourceError("edit plan has no matching editable text")
        mutations = tuple(
            self.mutation_factory.replacement_from_artifact(
                source.snapshot,
                scope.put_bytes(_bounded_output(after)),
            )
            for source, after in planned
        )
        return self._persist(
            request=request,
            discovery=discovery,
            sources=tuple(source for source, _ in planned),
            mutations=mutations,
            scope=scope,
            transaction_id=transaction_id,
        )

    def _plan_literal(
        self,
        request: LiteralEditPlanRequest,
        scope: ArtifactWriteScope,
        *,
        transaction_id: str | None,
    ) -> EditPlan:
        snapshot, raw, editable = self._observed_editable(request.path)
        spans = _literal_spans(editable.text, request.old)
        if not spans:
            raise EditPlanSourceError("string to replace not found in file")
        if not request.replace_all and len(spans) > 1:
            raise EditPlanSourceError(
                f"found {len(spans)} matches of the string to replace; " "provide a unique literal or set replace_all"
            )
        selected = spans if request.replace_all else spans[:1]
        after = _replace_literal_raw(
            raw,
            editable,
            selected,
            request.new,
        )
        source = ExistingEditPlanSource(
            snapshot=snapshot,
            newline_profile=editable.newline_profile,
            replacement_count=len(selected),
        )
        mutation = self.mutation_factory.replacement_from_artifact(
            snapshot,
            scope.put_bytes(_bounded_output(after)),
        )
        discovery = _single_file_discovery(request.path)
        return self._persist(
            request=request,
            discovery=discovery,
            sources=(source,),
            mutations=(mutation,),
            scope=scope,
            transaction_id=transaction_id,
        )

    def _plan_whole_file(
        self,
        request: WholeFileEditPlanRequest,
        scope: ArtifactWriteScope,
        *,
        transaction_id: str | None,
    ) -> EditPlan:
        snapshot = self.resolve_observed(request.path)
        if snapshot is not None:
            existing, mutation = self._whole_existing(request, snapshot, scope)
            return self._persist(
                request=request,
                discovery=_single_file_discovery(request.path),
                sources=(existing,),
                mutations=(mutation,),
                scope=scope,
                transaction_id=transaction_id,
            )
        absent, mutation = self._whole_absent(request, scope)
        return self._persist(
            request=request,
            discovery=_single_file_discovery(request.path),
            sources=(absent,),
            mutations=(mutation,),
            scope=scope,
            transaction_id=transaction_id,
        )

    def _whole_existing(
        self,
        request: WholeFileEditPlanRequest,
        snapshot: FileSnapshot,
        scope: ArtifactWriteScope,
    ) -> tuple[ExistingEditPlanSource, ReplaceMutation]:
        if request.encoding is not None:
            raise EditPlanSourceError("existing whole-file edits preserve the observed encoding")
        sealed, _, editable = self._observed_editable(
            request.path,
            snapshot=snapshot,
        )
        content = _normalize_newlines(
            request.content,
            editable.newline_profile.dominant,
        )
        after = _encode_existing(content, editable.encoding)
        source = ExistingEditPlanSource(
            snapshot=sealed,
            newline_profile=editable.newline_profile,
            replacement_count=1,
        )
        return source, self.mutation_factory.replacement_from_artifact(
            sealed,
            scope.put_bytes(_bounded_output(after)),
        )

    def _whole_absent(
        self,
        request: WholeFileEditPlanRequest,
        scope: ArtifactWriteScope,
    ) -> tuple[AbsentEditPlanSource, CreateMutation]:
        if os.path.isdir(request.path.native):
            raise EditPlanSourceError(f"edit target is a directory, not a file: {request.path.display}")
        if os.path.lexists(request.path.native):
            raise StaleSnapshotError(
                f"file has not been read this session: {request.path.display}",
                path=request.path.display,
            )
        raw, decision, newline_profile = _encode_new_file(
            request.content,
            request.encoding,
        )
        after = scope.put_bytes(_bounded_output(raw))
        mutation = self.mutation_factory.creation_from_artifact(
            request.path.native,
            after,
            scope=scope,
        )
        source = AbsentEditPlanSource(
            requested_path=mutation.requested_path,
            target_path=mutation.target_path,
            project_identity=mutation.project_identity,
            expected_version=mutation.expected_version,
            metadata=mutation.metadata,
            encoding=decision,
            newline_profile=newline_profile,
            replacement_count=1,
        )
        return source, mutation

    def _observed_editable(
        self,
        path: PathToken,
        *,
        snapshot: FileSnapshot | None = None,
    ):
        observed = snapshot or self.resolve_observed(path)
        if observed is None:
            if os.path.isdir(path.native):
                raise EditPlanSourceError(f"edit target is a directory, not a file: {path.display}")
            if not os.path.lexists(path.native):
                raise EditPlanSourceError(f"file does not exist: {path.display}")
            raise EditPlanSourceError(f"file has not been read this session: {path.display}")
        if observed.requested_path.native != path.native:
            raise EditPlanSourceError("observed snapshot path does not match edit path")
        if observed.encoding is None:
            raise EditPlanSourceError(f"observed snapshot has no sealed text encoding: {path.display}")
        self.artifacts.verify(observed.artifact)
        self.artifacts.verify(observed.metadata)
        raw = self.artifacts.read_bounded(
            observed.artifact,
            maximum_bytes=observed.version.size,
        )
        return observed, raw, editable_text(raw, observed.encoding)

    def _persist(
        self,
        *,
        request: EditPlanRequest,
        discovery: CandidateDiscovery,
        sources: tuple[EditPlanSource, ...],
        mutations: tuple[CreateMutation | ReplaceMutation, ...],
        scope: ArtifactWriteScope,
        transaction_id: str | None,
    ) -> EditPlan:
        review_facts = tuple(
            self._review_fact(source, mutation, scope) for source, mutation in zip(sources, mutations, strict=True)
        )
        identity = _identity_material(
            request,
            discovery,
            sources,
            review_facts,
            mutations,
        )
        plan_id = hashlib.sha256(b"mote-edit-plan\0" + identity).hexdigest()
        transaction_id = transaction_id or hashlib.sha256(b"mote-edit-transaction\0" + identity).hexdigest()
        mutation_set = self.mutation_factory.mutation_set(
            transaction_id=transaction_id,
            source="EditPlanner",
            mutations=mutations,
        )
        preview = EditPlanPreview(
            total_replacements=sum(source.replacement_count for source in sources),
            affected_files=tuple(
                EditPlanFilePreview(
                    path=_source_path(source),
                    replacement_count=source.replacement_count,
                )
                for source in sources
            ),
        )
        return self.store.persist(
            plan_id=plan_id,
            transaction_id=transaction_id,
            request=request,
            discovery=discovery,
            sources=sources,
            preview=preview,
            review_facts=review_facts,
            mutation_set=mutation_set,
            scope=scope,
        )

    def _review_fact(
        self,
        source: EditPlanSource,
        mutation: CreateMutation | ReplaceMutation,
        scope: ArtifactWriteScope,
    ) -> EditPlanReviewFact:
        if isinstance(source, ExistingEditPlanSource):
            before_raw = self.artifacts.read_bounded(
                source.snapshot.artifact,
                maximum_bytes=source.snapshot.version.size,
            )
            decision = source.snapshot.encoding
            if decision is None:
                raise EditPlanSourceError("existing edit source encoding is missing")
            before = editable_text(before_raw, decision).text
        else:
            decision = source.encoding
            before = ""
        after_raw = self.artifacts.read_bounded(
            mutation.after,
            maximum_bytes=MAX_EDIT_PLAN_OUTPUT_BYTES,
        )
        after = editable_text(after_raw, decision).text
        before_utf8 = _bounded_review_fact(_normalize_newlines(before, "\n").encode("utf-8"))
        after_utf8 = _bounded_review_fact(_normalize_newlines(after, "\n").encode("utf-8"))
        return EditPlanReviewFact(
            path=_source_path(source),
            before_utf8=scope.put_bytes(before_utf8),
            after_utf8=scope.put_bytes(after_utf8),
        )


def _replace_raw(raw, editable, program, matches, replacement: str) -> bytes:
    chunks: list[bytes] = []
    raw_cursor = 0
    for match in matches:
        raw_start = editable.logical_to_raw_boundaries[match.start()]
        raw_end = editable.logical_to_raw_boundaries[match.end()]
        chunks.append(raw[raw_cursor:raw_start])
        try:
            expanded = program.expand_replacement(match, replacement)
            expanded = _normalize_newlines(
                expanded,
                editable.newline_profile.dominant,
            )
            chunks.append(expanded.encode(editable.encoding.label, errors="strict"))
        except (UnicodeError, re.error) as exc:
            raise EncodingRejectedError(
                f"replacement cannot be represented as {editable.encoding.label}",
                encoding=editable.encoding.label,
                cause=exc,
            ) from exc
        raw_cursor = raw_end
    chunks.append(raw[raw_cursor:])
    return b"".join(chunks)


def _literal_spans(text: str, old: str) -> tuple[tuple[int, int], ...]:
    spans = []
    cursor = 0
    while True:
        start = text.find(old, cursor)
        if start < 0:
            return tuple(spans)
        end = start + len(old)
        spans.append((start, end))
        cursor = end


def _replace_literal_raw(raw, editable, spans, replacement: str) -> bytes:
    normalized = _normalize_newlines(
        replacement,
        editable.newline_profile.dominant,
    )
    try:
        encoded = normalized.encode(editable.encoding.label, errors="strict")
    except UnicodeError as exc:
        raise EncodingRejectedError(
            f"replacement cannot be represented as {editable.encoding.label}",
            encoding=editable.encoding.label,
            cause=exc,
        ) from exc
    chunks = []
    raw_cursor = 0
    for start, end in spans:
        raw_start = editable.logical_to_raw_boundaries[start]
        raw_end = editable.logical_to_raw_boundaries[end]
        chunks.append(raw[raw_cursor:raw_start])
        chunks.append(encoded)
        raw_cursor = raw_end
    chunks.append(raw[raw_cursor:])
    return b"".join(chunks)


def _normalize_newlines(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _encode_existing(text: str, decision: EncodingDecision) -> bytes:
    try:
        return decision.bom + text.encode(decision.label, errors="strict")
    except UnicodeError as exc:
        raise EncodingRejectedError(
            f"replacement cannot be represented as {decision.label}",
            encoding=decision.label,
            cause=exc,
        ) from exc


def _encode_new_file(
    text: str,
    encoding: str | None,
) -> tuple[bytes, EncodingDecision, NewlineProfile]:
    label = encoding or "utf-8"
    try:
        raw = text.encode(label, errors="strict")
    except (LookupError, UnicodeError) as exc:
        raise EncodingRejectedError(
            f"new file content cannot be represented as {label}",
            encoding=label,
            cause=exc,
        ) from exc
    _, decision = decode_text(raw, explicit=label)
    editable = editable_text(raw, decision)
    return raw, decision, editable.newline_profile


def _bounded_output(raw: bytes) -> bytes:
    if len(raw) > MAX_EDIT_PLAN_OUTPUT_BYTES:
        raise EditPlanOutputLimitError(MAX_EDIT_PLAN_OUTPUT_BYTES, len(raw))
    return raw


def _bounded_review_fact(raw: bytes) -> bytes:
    if len(raw) > MAX_EDIT_PLAN_REVIEW_FACT_BYTES:
        raise EditPlanOutputLimitError(MAX_EDIT_PLAN_REVIEW_FACT_BYTES, len(raw))
    return raw


def _single_file_discovery(path: PathToken) -> CandidateDiscovery:
    request = CandidateDiscoveryRequest(root=path)
    return CandidateDiscovery.freeze(request, (path,))


def _source_path(source: EditPlanSource) -> PathToken:
    if isinstance(source, ExistingEditPlanSource):
        return source.snapshot.requested_path
    return source.requested_path


def _identity_material(request, discovery, sources, review_facts, mutations) -> bytes:
    return _canonical_json(
        {
            "request": _request_to_dict(request),
            "discovery": _discovery_to_dict(discovery),
            "sources": [_source_to_dict(source) for source in sources],
            "review_facts": [_review_fact_to_dict(fact) for fact in review_facts],
            "mutations": [mutation_to_dict(mutation) for mutation in mutations],
        }
    )


def _plan_payload(**values) -> dict[str, Any]:
    return {
        "format_version": _FORMAT_VERSION,
        "plan_id": values["plan_id"],
        "transaction_id": values["transaction_id"],
        "request": blob_to_dict(values["request_artifact"]),
        "discovery": _discovery_to_dict(values["discovery"]),
        "sources": [_source_to_dict(source) for source in values["sources"]],
        "preview": {
            "total_replacements": values["preview"].total_replacements,
            "affected_files": [
                {
                    "path": path_to_dict(item.path),
                    "replacement_count": item.replacement_count,
                }
                for item in values["preview"].affected_files
            ],
        },
        "review_facts": [_review_fact_to_dict(fact) for fact in values["review_facts"]],
        "mutation_set": mutation_set_to_dict(values["mutation_set"]),
    }


def _plan_from_payload(
    payload: Any,
    manifest: ContentIdentity,
    artifacts: FileMutationArtifactRepository,
) -> EditPlan:
    if type(payload) is not dict or set(payload) != _MANIFEST_KEYS:
        raise EditPlanManifestError("edit plan fields are not canonical")
    if type(payload["format_version"]) is not int or payload["format_version"] != _FORMAT_VERSION:
        raise ValueError("unsupported edit plan format")
    plan_id = _strict_text(payload["plan_id"], "plan id")
    transaction_id = _strict_text(payload["transaction_id"], "transaction id")
    request_artifact = blob_from_dict(payload["request"])
    if request_artifact is None:
        raise ValueError("edit plan request artifact is missing")
    request_payload = json.loads(
        artifacts.read_bounded(
            request_artifact,
            maximum_bytes=MAX_EDIT_PLAN_REQUEST_BYTES,
        ).decode("ascii", errors="strict")
    )
    request = _request_from_dict(request_payload)
    discovery = _discovery_from_dict(payload["discovery"])
    if type(payload["sources"]) is not list or not payload["sources"]:
        raise ValueError("edit plan sources are invalid")
    sources = tuple(_source_from_dict(item) for item in payload["sources"])
    preview = _preview_from_dict(payload["preview"])
    if type(payload["review_facts"]) is not list or not payload["review_facts"]:
        raise ValueError("edit plan review facts are invalid")
    review_facts = tuple(_review_fact_from_dict(item) for item in payload["review_facts"])
    mutation_set = mutation_set_from_dict(payload["mutation_set"])
    return EditPlan(
        plan_id=plan_id,
        transaction_id=transaction_id,
        manifest_artifact=manifest,
        request_artifact=request_artifact,
        request=request,
        discovery=discovery,
        sources=sources,
        preview=preview,
        review_facts=review_facts,
        mutation_set=mutation_set,
    )


def _validate_plan_identity(plan: EditPlan) -> None:
    mutations = plan.mutation_set.mutations
    if plan.transaction_id != plan.mutation_set.transaction_id:
        raise ValueError("edit plan transaction identity is inconsistent")
    if len(plan.sources) != len(mutations):
        raise ValueError("edit plan sources do not match mutations")
    if len(plan.review_facts) != len(mutations):
        raise ValueError("edit plan review facts do not match mutations")
    for source, mutation in zip(plan.sources, mutations, strict=True):
        if isinstance(source, ExistingEditPlanSource):
            if not isinstance(mutation, ReplaceMutation):
                raise ValueError("existing edit source requires a replace mutation")
            if source.snapshot != mutation.before:
                raise ValueError("existing edit source snapshot is inconsistent")
        else:
            if not isinstance(mutation, CreateMutation):
                raise ValueError("absent edit source requires a create mutation")
            if (
                source.requested_path != mutation.requested_path
                or source.target_path != mutation.target_path
                or source.project_identity != mutation.project_identity
                or source.expected_version != mutation.expected_version
                or source.metadata != mutation.metadata
            ):
                raise ValueError("absent edit source is inconsistent")
    identity = _identity_material(
        plan.request,
        plan.discovery,
        plan.sources,
        plan.review_facts,
        mutations,
    )
    expected_plan = hashlib.sha256(b"mote-edit-plan\0" + identity).hexdigest()
    expected_transaction = hashlib.sha256(b"mote-edit-transaction\0" + identity).hexdigest()
    if plan.plan_id != expected_plan or plan.transaction_id != expected_transaction:
        raise ValueError("edit plan identity digest is invalid")
    if plan.preview.total_replacements != sum(source.replacement_count for source in plan.sources):
        raise ValueError("edit plan preview count is inconsistent")
    expected_preview = tuple(
        EditPlanFilePreview(
            path=_source_path(source),
            replacement_count=source.replacement_count,
        )
        for source in plan.sources
    )
    if plan.preview.affected_files != expected_preview:
        raise ValueError("edit plan preview files are inconsistent")
    if tuple(fact.path for fact in plan.review_facts) != tuple(item.path for item in expected_preview):
        raise ValueError("edit plan review fact paths are inconsistent")


def _validate_plan_artifacts(
    plan: EditPlan,
    artifacts: FileMutationArtifactRepository,
) -> None:
    for source, mutation, fact in zip(
        plan.sources,
        plan.mutation_set.mutations,
        plan.review_facts,
        strict=True,
    ):
        if isinstance(source, ExistingEditPlanSource):
            artifacts.read_bounded(
                source.snapshot.artifact,
                maximum_bytes=source.snapshot.version.size,
            )
            metadata = source.snapshot.metadata
        else:
            metadata = source.metadata
        artifacts.read_bounded(
            metadata,
            maximum_bytes=MAX_METADATA_MANIFEST_BYTES,
        )
        if not isinstance(mutation, DeleteMutation):
            artifacts.read_bounded(
                mutation.after,
                maximum_bytes=MAX_EDIT_PLAN_OUTPUT_BYTES,
            )
        artifacts.read_bounded(
            fact.before_utf8,
            maximum_bytes=MAX_EDIT_PLAN_REVIEW_FACT_BYTES,
        )
        artifacts.read_bounded(
            fact.after_utf8,
            maximum_bytes=MAX_EDIT_PLAN_REVIEW_FACT_BYTES,
        )


def _request_to_dict(request: EditPlanRequest) -> dict[str, Any]:
    if isinstance(request, RegexEditPlanRequest):
        return {
            "kind": request.kind.value,
            "root": path_to_dict(request.root),
            "pattern": request.pattern,
            "replacement": request.replacement,
            "globs": list(request.globs),
            "type_name": request.type_name,
            "case_insensitive": request.case_insensitive,
            "multiline": request.multiline,
            "encoding": request.encoding,
            "fallback_encoding": request.fallback_encoding,
            "max_replacements": request.max_replacements,
            "timeout": request.timeout,
        }
    if isinstance(request, LiteralEditPlanRequest):
        return {
            "kind": request.kind.value,
            "path": path_to_dict(request.path),
            "old": request.old,
            "new": request.new,
            "replace_all": request.replace_all,
        }
    return {
        "kind": request.kind.value,
        "path": path_to_dict(request.path),
        "content": request.content,
        "encoding": request.encoding,
    }


def _request_from_dict(payload: Any) -> EditPlanRequest:
    if type(payload) is not dict or type(payload.get("kind")) is not str:
        raise ValueError("edit plan request fields are not canonical")
    kind = EditPlanRequestKind(payload["kind"])
    if kind == EditPlanRequestKind.REGEX:
        keys = {
            "kind",
            "root",
            "pattern",
            "replacement",
            "globs",
            "type_name",
            "case_insensitive",
            "multiline",
            "encoding",
            "fallback_encoding",
            "max_replacements",
            "timeout",
        }
        if set(payload) != keys:
            raise ValueError("regex edit request fields are not canonical")
        if type(payload["globs"]) is not list or any(type(item) is not str for item in payload["globs"]):
            raise ValueError("edit plan globs are invalid")
        return RegexEditPlanRequest(
            root=path_from_dict(payload["root"]),
            pattern=_strict_text(payload["pattern"], "pattern", allow_empty=True),
            replacement=_strict_text(payload["replacement"], "replacement", allow_empty=True),
            globs=tuple(payload["globs"]),
            type_name=_strict_text(payload["type_name"], "type name", allow_empty=True),
            case_insensitive=_strict_bool(payload["case_insensitive"]),
            multiline=_strict_bool(payload["multiline"]),
            encoding=_optional_text(payload["encoding"]),
            fallback_encoding=_optional_text(payload["fallback_encoding"]),
            max_replacements=_positive_int(payload["max_replacements"]),
            timeout=_positive_number(payload["timeout"]),
        )
    if kind == EditPlanRequestKind.LITERAL:
        if set(payload) != {"kind", "path", "old", "new", "replace_all"}:
            raise ValueError("literal edit request fields are not canonical")
        return LiteralEditPlanRequest(
            path=path_from_dict(payload["path"]),
            old=_strict_text(payload["old"], "old text"),
            new=_strict_text(payload["new"], "new text", allow_empty=True),
            replace_all=_strict_bool(payload["replace_all"]),
        )
    if set(payload) != {"kind", "path", "content", "encoding"}:
        raise ValueError("whole-file edit request fields are not canonical")
    return WholeFileEditPlanRequest(
        path=path_from_dict(payload["path"]),
        content=_strict_text(payload["content"], "content", allow_empty=True),
        encoding=_optional_text(payload["encoding"]),
    )


def _discovery_to_dict(discovery: CandidateDiscovery) -> dict[str, Any]:
    return {
        "request": {
            "root": path_to_dict(discovery.request.root),
            "globs": list(discovery.request.globs),
            "type_name": discovery.request.type_name,
        },
        "candidates": [path_to_dict(path) for path in discovery.candidates],
    }


def _discovery_from_dict(payload: Any) -> CandidateDiscovery:
    if type(payload) is not dict or set(payload) != {"request", "candidates"}:
        raise ValueError("edit plan discovery fields are not canonical")
    request = payload["request"]
    if type(request) is not dict or set(request) != {"root", "globs", "type_name"}:
        raise ValueError("edit plan discovery request is invalid")
    if type(request["globs"]) is not list or any(type(item) is not str for item in request["globs"]):
        raise ValueError("edit plan discovery globs are invalid")
    if type(payload["candidates"]) is not list:
        raise ValueError("edit plan candidates are invalid")
    return CandidateDiscovery(
        request=CandidateDiscoveryRequest(
            root=path_from_dict(request["root"]),
            globs=tuple(request["globs"]),
            type_name=_strict_text(request["type_name"], "type name", allow_empty=True),
        ),
        candidates=tuple(path_from_dict(item) for item in payload["candidates"]),
    )


def _source_to_dict(source: EditPlanSource) -> dict[str, Any]:
    if isinstance(source, ExistingEditPlanSource):
        return {
            "kind": source.kind.value,
            "snapshot": snapshot_to_dict(source.snapshot),
            "newline_profile": _newline_to_dict(source.newline_profile),
            "replacement_count": source.replacement_count,
        }
    return {
        "kind": source.kind.value,
        "requested_path": path_to_dict(source.requested_path),
        "target_path": path_to_dict(source.target_path),
        "project_identity": {
            "key": source.project_identity.key,
            "scheme": source.project_identity.scheme,
        },
        "expected_version": version_to_dict(source.expected_version),
        "metadata": blob_to_dict(source.metadata),
        "encoding": encoding_to_dict(source.encoding),
        "newline_profile": _newline_to_dict(source.newline_profile),
        "replacement_count": source.replacement_count,
    }


def _source_from_dict(payload: Any) -> EditPlanSource:
    if type(payload) is not dict or type(payload.get("kind")) is not str:
        raise ValueError("edit plan source fields are not canonical")
    kind = EditSourceKind(payload["kind"])
    if kind == EditSourceKind.EXISTING_PLAN:
        if set(payload) != {
            "kind",
            "snapshot",
            "newline_profile",
            "replacement_count",
        }:
            raise ValueError("existing edit source fields are not canonical")
        return ExistingEditPlanSource(
            snapshot=snapshot_from_dict(payload["snapshot"]),
            newline_profile=_newline_from_dict(payload["newline_profile"]),
            replacement_count=_positive_int(payload["replacement_count"]),
        )
    if kind != EditSourceKind.ABSENT_PLAN or set(payload) != {
        "kind",
        "requested_path",
        "target_path",
        "project_identity",
        "expected_version",
        "metadata",
        "encoding",
        "newline_profile",
        "replacement_count",
    }:
        raise ValueError("absent edit source fields are not canonical")
    project = payload["project_identity"]
    if (
        type(project) is not dict
        or set(project) != {"key", "scheme"}
        or type(project["key"]) is not str
        or not project["key"]
        or type(project["scheme"]) is not str
        or not project["scheme"]
    ):
        raise ValueError("absent edit source project identity is invalid")
    expected = version_from_dict(payload["expected_version"])
    metadata = blob_from_dict(payload["metadata"])
    encoding = encoding_from_dict(payload["encoding"])
    if not isinstance(expected, AbsentVersion) or metadata is None or encoding is None:
        raise ValueError("absent edit source state is invalid")
    return AbsentEditPlanSource(
        requested_path=path_from_dict(payload["requested_path"]),
        target_path=path_from_dict(payload["target_path"]),
        project_identity=ProjectIdentity(
            key=project["key"],
            scheme=project["scheme"],
        ),
        expected_version=expected,
        metadata=metadata,
        encoding=encoding,
        newline_profile=_newline_from_dict(payload["newline_profile"]),
        replacement_count=_positive_int(payload["replacement_count"]),
    )


def _newline_to_dict(profile: NewlineProfile) -> dict[str, int]:
    return {"lf": profile.lf, "crlf": profile.crlf, "cr": profile.cr}


def _newline_from_dict(payload: Any) -> NewlineProfile:
    if type(payload) is not dict or set(payload) != {"lf", "crlf", "cr"}:
        raise ValueError("edit plan newline profile fields are not canonical")
    for field in ("lf", "crlf", "cr"):
        if type(payload[field]) is not int or payload[field] < 0:
            raise ValueError("edit plan newline profile is invalid")
    return NewlineProfile(
        lf=payload["lf"],
        crlf=payload["crlf"],
        cr=payload["cr"],
    )


def _review_fact_to_dict(fact: EditPlanReviewFact) -> dict[str, Any]:
    return {
        "path": path_to_dict(fact.path),
        "before_utf8": blob_to_dict(fact.before_utf8),
        "after_utf8": blob_to_dict(fact.after_utf8),
    }


def _review_fact_from_dict(payload: Any) -> EditPlanReviewFact:
    if type(payload) is not dict or set(payload) != {
        "path",
        "before_utf8",
        "after_utf8",
    }:
        raise ValueError("edit plan review fact fields are not canonical")
    before = blob_from_dict(payload["before_utf8"])
    after = blob_from_dict(payload["after_utf8"])
    if before is None or after is None:
        raise ValueError("edit plan review fact artifacts are missing")
    return EditPlanReviewFact(
        path=path_from_dict(payload["path"]),
        before_utf8=before,
        after_utf8=after,
    )


def _preview_from_dict(payload: Any) -> EditPlanPreview:
    if type(payload) is not dict or set(payload) != {
        "total_replacements",
        "affected_files",
    }:
        raise ValueError("edit plan preview fields are not canonical")
    if type(payload["affected_files"]) is not list:
        raise ValueError("edit plan preview files are invalid")
    files = []
    for item in payload["affected_files"]:
        if type(item) is not dict or set(item) != {"path", "replacement_count"}:
            raise ValueError("edit plan preview file is invalid")
        files.append(
            EditPlanFilePreview(
                path=path_from_dict(item["path"]),
                replacement_count=_positive_int(item["replacement_count"]),
            )
        )
    return EditPlanPreview(
        total_replacements=_positive_int(payload["total_replacements"]),
        affected_files=tuple(files),
    )


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _strict_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise ValueError(f"edit plan {label} is invalid")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _strict_text(value, "optional text")


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("edit plan boolean is invalid")
    return value


def _positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("edit plan positive integer is invalid")
    return value


def _positive_number(value: Any) -> float:
    if type(value) not in (int, float) or isinstance(value, bool) or value <= 0:
        raise ValueError("edit plan positive number is invalid")
    return float(value)


__all__ = [
    "MAX_EDIT_PLAN_MANIFEST_BYTES",
    "MAX_EDIT_PLAN_OUTPUT_BYTES",
    "MAX_EDIT_PLAN_REQUEST_BYTES",
    "AbsentEditPlanSource",
    "EditPlan",
    "EditPlanFilePreview",
    "EditPlanManifestError",
    "EditPlanOutputLimitError",
    "EditPlanPreview",
    "EditPlanRequest",
    "EditPlanReviewFact",
    "EditPlanSource",
    "EditPlanSourceError",
    "EditPlanStore",
    "EditPlanner",
    "ExistingEditPlanSource",
    "LiteralEditPlanRequest",
    "RegexEditPlanRequest",
    "ReplacementLimitExceededError",
    "WholeFileEditPlanRequest",
]
