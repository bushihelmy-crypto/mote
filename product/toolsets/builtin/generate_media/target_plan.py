"""Product-owned deterministic target planning for generated media."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence


class MediaTargetDisposition(StrEnum):
    REQUESTED = "requested"
    RENAMED = "renamed"


class MediaPublicationDisposition(StrEnum):
    COMMITTED = "committed"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class MediaTargetPlan:
    item_id: str
    kind: str
    index: int
    requested_target: str
    resolved_target: str
    disposition: MediaTargetDisposition

    def to_payload(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "index": self.index,
            "requested_target": self.requested_target,
            "resolved_target": self.resolved_target,
            "target_disposition": self.disposition.value,
        }


@dataclass(frozen=True, slots=True)
class MediaPublicationSettlement:
    plan: MediaTargetPlan
    disposition: MediaPublicationDisposition
    detail: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            **self.plan.to_payload(),
            "publication_disposition": self.disposition.value,
            "detail": self.detail,
        }


def plan_media_targets(
    *,
    cwd: str,
    output_dir: str,
    items_by_kind: Sequence[tuple[str, Sequence[Mapping[str, object]]]],
    collision_round: int = 1,
) -> tuple[MediaTargetPlan, ...]:
    """Resolve every requested target before authorization or remote effects."""
    directory = Path(output_dir).expanduser()
    if not directory.is_absolute():
        directory = Path(cwd) / directory
    directory = Path(os.path.abspath(directory))
    if isinstance(collision_round, bool) or collision_round < 1:
        raise ValueError("collision_round must be positive")
    used: set[str] = set()
    plans: list[MediaTargetPlan] = []
    for kind, items in items_by_kind:
        for index, item in enumerate(items):
            requested_name = Path(str(item.get("filename") or _default_filename(kind))).name
            requested = str(directory / requested_name)
            resolved_name = requested_name if collision_round == 1 else _with_suffix(requested_name, collision_round)
            ordinal = collision_round
            while os.path.normcase(str(directory / resolved_name)) in used:
                ordinal += 1
                resolved_name = _with_suffix(requested_name, ordinal)
            resolved = str(directory / resolved_name)
            used.add(os.path.normcase(resolved))
            plans.append(
                MediaTargetPlan(
                    item_id=f"{kind}:{index}",
                    kind=kind,
                    index=index,
                    requested_target=requested,
                    resolved_target=resolved,
                    disposition=(
                        MediaTargetDisposition.REQUESTED if requested == resolved else MediaTargetDisposition.RENAMED
                    ),
                )
            )
    return tuple(plans)


def _with_suffix(filename: str, ordinal: int) -> str:
    path = Path(filename)
    suffix = "".join(path.suffixes)
    stem = filename[: -len(suffix)] if suffix else filename
    return f"{stem}-{ordinal}{suffix}"


def _default_filename(kind: str) -> str:
    return {
        "image": "image.png",
        "audio": "audio.mp3",
        "music": "music.wav",
        "video": "video.mp4",
    }[kind]


__all__ = [
    "MediaPublicationDisposition",
    "MediaPublicationSettlement",
    "MediaTargetDisposition",
    "MediaTargetPlan",
    "plan_media_targets",
]
