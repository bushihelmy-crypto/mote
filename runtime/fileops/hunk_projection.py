"""Deterministic review hunks projected from sealed UTF-8 edit-plan facts."""

from __future__ import annotations

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.mutations import MutationSet
from mote.contracts.file.transactions import HunkRecord
from mote.runtime.fileops.edit_plans import EditPlanReviewFact
from mote.runtime.fileops.hunks import split_hunks
from mote.runtime.fileops.mutation.artifacts import FileMutationArtifactRepository


class EditPlanHunkProjector:
    """Build review records without reopening or re-decoding live files."""

    def __init__(self, *, session_id: str, artifacts: FileMutationArtifactRepository) -> None:
        self.session_id = session_id
        self.artifacts = artifacts

    def project(
        self,
        mutation_set: MutationSet,
        review_facts: tuple[EditPlanReviewFact, ...],
        *,
        turn_index: int | None,
    ) -> tuple[HunkRecord, ...]:
        if turn_index is None:
            return ()
        if len(review_facts) != len(mutation_set.mutations):
            raise ValueError("edit plan review facts do not match mutations")
        records: list[HunkRecord] = []
        for mutation_index, (mutation, fact) in enumerate(zip(mutation_set.mutations, review_facts, strict=True)):
            if fact.path != mutation.requested_path:
                raise ValueError("edit plan review fact path does not match mutation")
            before = self._read_text(fact.before_utf8)
            after = self._read_text(fact.after_utf8)
            for hunk_index, hunk in enumerate(split_hunks(before, after)):
                records.append(
                    HunkRecord(
                        hunk_id=(f"{mutation_set.transaction_id}:" f"{mutation_index}:{hunk_index}"),
                        path=fact.path.display,
                        session_id=self.session_id,
                        tool_call_id="",
                        turn_index=turn_index,
                        source="agent",
                        old_range=(hunk.old_start, hunk.old_count),
                        new_range=(hunk.new_start, hunk.new_count),
                        pre_hash=fact.before_utf8.digest,
                        post_hash=fact.after_utf8.digest,
                        expected_digest=mutation.after.digest,
                    )
                )
        return tuple(records)

    def _read_text(self, artifact: ContentIdentity) -> str:
        raw = self.artifacts.read_bytes(artifact)
        text = raw.decode("utf-8", errors="strict")
        if "\r" in text:
            raise ValueError("edit plan review facts must use normalized LF text")
        return text


__all__ = ["EditPlanHunkProjector"]
