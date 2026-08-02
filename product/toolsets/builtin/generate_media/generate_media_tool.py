"""``GenerateMedia`` — one-call multimedia generation (image, speech, music, video).

A direct fan-out tool (NOT a graph orchestrator): the model passes explicit
per-asset lists and this generates all four kinds concurrently, blocking until
every asset resolves to its final URL, then returns one compact result. It does
NOT plan assets with a storyboard LLM or auto-compose a final clip — the model
itself decides what to generate. All four list params are native-channel only
(the XML protocol delivers args as strings); omit any kind you don't need.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, ClassVar, Optional

import aiohttp

from mote.contracts.authorization import PermissionFacts
from mote.contracts.file import RecoveryInDoubtError, TransactionStatus
from mote.contracts.ports.file.operations import GeneratedTargetReservationPort
from mote.contracts.service import (
    MediaGenerationPayload,
    MediaGenerationResult,
    MediaGenerationSpec,
    MediaKind,
    ServiceExecutionSemantics,
)
from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.errors import ToolNotConfiguredError
from mote.contracts.tool.identity import ToolInvocationIdentity, tool_arguments_digest
from mote.contracts.tool.result import json_tool_payload
from mote.product.toolsets.builtin.generate_media.target_plan import (
    MediaPublicationDisposition,
    MediaPublicationSettlement,
    MediaTargetPlan,
    plan_media_targets,
)
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import CommitGeneratedFiles, GetCwd, InvokeService, TryReserveGeneratedTargets
from mote.runtime.tools.execution_context import current_authorized_invocation
from mote.runtime.tools.tool_result import ToolResult

# Requested-kind -> (multimodal sub-config attribute, human label, model-field
# names). The tool refuses a kind up-front when its service endpoint/key is
# unconfigured OR its generation model is unset, turning a would-be upstream 4xx
# into a clear ToolNotConfiguredError naming the exact config path
# (multimodal.<attr>). Video carries TWO model fields (text-to-video +
# reference-guided); the rest carry a single ``model``.
_KIND_CONFIG: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "images": ("image_generation", "image", ("model",)),
    "audios": ("audio_generation", "speech/TTS", ("model",)),
    "music": ("music_generation", "music", ("model",)),
    "videos": (
        "video_generation",
        "video",
        ("text_to_video_model", "reference_guided_video_model"),
    ),
}


def _check_configured(multimodal: Any, kinds: list[str]) -> None:
    """Raise ToolNotConfiguredError if any requested *kind* is not usable.

    A media service is usable only once its ``base_url`` and ``api_key`` are
    filled AND its generation model field(s) name a model. An empty endpoint/key
    or an unset model would fail deep inside an HTTP call with an opaque error —
    this front-loads it into an actionable notice pointing at ``multimodal.<attr>``.
    """
    missing: list[str] = []
    for kind in kinds:
        attr, label, model_fields = _KIND_CONFIG[kind]
        cfg = getattr(multimodal, attr)
        if not (cfg.base_url and cfg.api_key):
            missing.append(f"{label} (set multimodal.{attr}.base_url + .api_key)")
            continue
        # Endpoint/key present but no model named → the service can't pick a
        # model to generate with. Surface it the same way (model not configured).
        unset = [f for f in model_fields if not getattr(cfg, f, "")]
        if unset:
            paths = " + ".join(f"multimodal.{attr}.{f}" for f in unset)
            missing.append(f"{label} (no model configured — set {paths})")
    if missing:
        raise ToolNotConfiguredError("Media generation service not configured for: " + "; ".join(missing) + ".")


class GenerateMedia(BaseTool):
    """Generate media assets — images, speech (TTS), music, and video — in one call."""

    name = "GenerateMedia"
    aliases: list[str] = ["generate_media"]
    requires: ClassVar[tuple[str, ...]] = (
        "invoke_service",
        "get_cwd",
        "commit_generated_files",
        "try_reserve_generated_targets",
    )
    effect = ToolEffect.EXTERNAL
    invoke_service: InvokeService
    get_cwd: GetCwd
    commit_generated_files: CommitGeneratedFiles
    try_reserve_generated_targets: TryReserveGeneratedTargets
    # Recall synonyms for tool-search: common ways a model asks for media work
    # that the summary line does not spell out.
    keywords: ClassVar[list[str]] = [
        "audio",
        "voice",
        "speech",
        "tts",
        "narration",
        "sound",
        "music",
        "song",
        "soundtrack",
        "image",
        "picture",
        "illustration",
        "video",
        "clip",
        "animation",
        "多媒体",
        "图片",
        "配音",
        "语音",
        "音频",
        "音乐",
        "视频",
        "生成图",
        "生成视频",
    ]
    # Batch generation escapes to remote APIs and may download to disk — a
    # one-shot side effect that must not be blindly replayed, so it stays the
    # conservative EXTERNAL (the default derivation), NOT reconstructable.

    def __init__(
        self,
        multimodal_config: Any,
    ) -> None:
        super().__init__()
        self._multimodal_config = multimodal_config
        self._target_reservations: dict[
            str,
            tuple[str, tuple[MediaTargetPlan, ...], GeneratedTargetReservationPort],
        ] = {}

    def can_resume_started_call(self, call_id: str) -> bool:
        """Re-enter the gateway, which resumes receipts instead of resubmitting."""
        return True

    def mutates_filesystem_for(self, args: dict) -> bool:
        return bool(args.get("output_dir"))

    def permission_targets(self, args: dict) -> list[str]:
        return [plan.resolved_target for plan in self._target_plan(args)]

    def permission_facts(
        self,
        arguments: dict[str, Any],
        identity: ToolInvocationIdentity,
    ) -> PermissionFacts:
        key = str(identity.invocation_id)
        digest = tool_arguments_digest(arguments)
        prior = self._target_reservations.get(key)
        if prior is not None and prior[0] == digest:
            return self._facts(arguments, prior[1])
        if prior is not None:
            prior[2].release()
            del self._target_reservations[key]
        output_dir = arguments.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir:
            return self._facts(arguments, ())
        for collision_round in range(1, 1_001):
            plans = self._target_plan(arguments, collision_round=collision_round)
            reservation = self.try_reserve_generated_targets(tuple(plan.resolved_target for plan in plans))
            if reservation is not None:
                self._target_reservations[key] = (digest, plans, reservation)
                return self._facts(arguments, plans)
        raise RuntimeError("generated media target reservation space is exhausted")

    def release_permission_facts(self, identity: ToolInvocationIdentity) -> None:
        prepared = self._target_reservations.pop(str(identity.invocation_id), None)
        if prepared is not None:
            prepared[2].release()

    def _facts(
        self,
        arguments: dict[str, Any],
        plans: tuple[MediaTargetPlan, ...],
    ) -> PermissionFacts:
        return PermissionFacts(
            targets=[plan.resolved_target for plan in plans],
            mutates_fs=bool(plans),
            tool_check=self.check_permissions(arguments),
            segments=self.permission_segments(arguments),
        )

    def _target_plan(
        self,
        args: dict,
        *,
        collision_round: int = 1,
    ) -> tuple[MediaTargetPlan, ...]:
        output_dir = args.get("output_dir")
        if not isinstance(output_dir, str) or not output_dir:
            return ()
        return plan_media_targets(
            cwd=self.get_cwd(),
            output_dir=output_dir,
            items_by_kind=tuple(
                (kind, items)
                for kind, items in (
                    ("image", args.get("images") or ()),
                    ("audio", args.get("audios") or ()),
                    ("music", args.get("music") or ()),
                    ("video", args.get("videos") or ()),
                )
            ),
            collision_round=collision_round,
        )

    async def call(
        self,
        *,
        images: Optional[list[dict]] = None,
        audios: Optional[list[dict]] = None,
        music: Optional[list[dict]] = None,
        videos: Optional[list[dict]] = None,
        output_dir: Optional[str] = None,
    ) -> dict | ToolResult:
        """Generate images, speech, music, and/or video assets and wait for the URLs.

        Runs every requested kind concurrently, blocks until all assets finish,
        then returns each asset's URL (and local path if ``output_dir`` is set).
        Omit any kind you don't need. Partial successes are kept; fails only when
        every asset failed.

        Args:
            images: Image specs, each ``{description, filename, size?, image?}``.
                ``image`` is a reference URL/path for image-to-image editing.
            audios: Speech (TTS) specs, each ``{text, filename, gender?, speed?}``.
                ``gender`` is "male"/"female" (voice selection).
            music: Music specs, each ``{prompt, filename, lyrics?, seed?}``.
            videos: Video specs, each ``{prompt, filename, size?, seconds?, image?}``.
                ``image`` (or ``first_frame``) is a reference frame.
            output_dir: Directory to download assets into. Omit to return only
                remote URLs (no local files).
        """
        requested = [
            kind
            for kind, items in (
                ("images", images),
                ("audios", audios),
                ("music", music),
                ("videos", videos),
            )
            if items
        ]
        if not requested:
            return {"message": "No media requested — pass at least one of images/audios/music/videos."}

        _check_configured(self._multimodal_config, requested)

        invocation = current_authorized_invocation()
        if output_dir and invocation is None:
            raise RuntimeError("GenerateMedia requires an authorized ToolExecutor invocation")
        invocation_id = "" if invocation is None else str(invocation.identity.invocation_id)
        prepared = self._target_reservations.get(invocation_id)
        if output_dir and prepared is None:
            raise RuntimeError("GenerateMedia target plan was not reserved during authorization")
        plans = () if prepared is None else prepared[1]
        plans_by_kind = {
            kind: {plan.index: plan for plan in plans if plan.kind == kind}
            for kind in ("image", "audio", "music", "video")
        }
        jobs: list[tuple[str, Any]] = []
        for plural, singular, items in (
            ("images", "image", images),
            ("audios", "audio", audios),
            ("music", "music", music),
            ("videos", "video", videos),
        ):
            if items:
                jobs.append(
                    (
                        plural,
                        self._generate_kind(
                            singular,
                            plural,
                            items,
                            target_plans=plans_by_kind[singular],
                            invocation_id=invocation_id,
                        ),
                    )
                )

        settled = await asyncio.gather(*(coro for _, coro in jobs), return_exceptions=True)

        out: dict[str, Any] = {}
        ok = 0
        for (kind, _), outcome in zip(jobs, settled):
            if isinstance(outcome, BaseException):
                out[kind] = {"error": str(outcome)}
            else:
                out[kind] = _compact(outcome)
                ok += 1
        if ok == 0:
            detail = "; ".join(f"{k}: {v.get('error', 'unknown error')}" for k, v in out.items())
            raise RuntimeError(f"All media generation failed: {detail}")
        if not plans:
            return out
        settlements = tuple(
            item["publication"]
            for value in out.values()
            if isinstance(value, dict)
            for item in value.get("assets", ())
            if "publication" in item
        )
        renamed = [item for item in settlements if item["target_disposition"] == "renamed"]
        notice = "Media generation completed."
        if renamed:
            notice += (
                " Renamed targets: "
                + ", ".join(f"{item['requested_target']} -> {item['resolved_target']}" for item in renamed)
                + "."
            )
        return ToolResult(output=notice, payload=json_tool_payload(out))

    async def _generate_kind(
        self,
        kind: str,
        plural: str,
        items: list[dict],
        *,
        target_plans: dict[int, MediaTargetPlan],
        invocation_id: str,
    ) -> dict[str, Any]:
        async def generate_one(index: int, item: dict) -> dict[str, Any]:
            filename = str(item.get("filename") or _default_filename(kind))
            plan = target_plans.get(index)
            try:
                value = await self.invoke_service(
                    MediaGenerationPayload(
                        media_kind=MediaKind(kind),
                        item=MediaGenerationSpec.model_validate(item),
                    ),
                    _generation_operation_key(kind, index, plan),
                    ServiceExecutionSemantics.IDEMPOTENT,
                )
                if not isinstance(value, MediaGenerationResult):
                    raise TypeError("media service returned a non-media response")
                result = value.model_dump(mode="json", exclude={"kind"})
                if plan is not None:
                    try:
                        content = await _download(result)
                    except Exception as exc:  # noqa: BLE001 - preserve accepted provider outcome
                        settlement = MediaPublicationSettlement(
                            plan,
                            MediaPublicationDisposition.FAILED,
                            str(exc),
                        )
                        result["publication"] = settlement.to_payload()
                        result["status"] = MediaPublicationDisposition.FAILED.value
                        result["materialization_error"] = settlement.detail
                        return result
                    try:
                        materialized = await self.commit_generated_files(
                            {plan.resolved_target: content},
                            source=f"GenerateMedia:{plan.item_id}",
                            transaction_id=_publication_transaction_id(invocation_id, plan.item_id),
                        )
                        disposition = {
                            TransactionStatus.COMMITTED: MediaPublicationDisposition.COMMITTED,
                            TransactionStatus.ABORTED: MediaPublicationDisposition.FAILED,
                            TransactionStatus.PREPARED: MediaPublicationDisposition.IN_DOUBT,
                            TransactionStatus.IN_DOUBT: MediaPublicationDisposition.IN_DOUBT,
                        }[materialized.status]
                        detail = materialized.detail
                    except RecoveryInDoubtError as exc:
                        disposition = MediaPublicationDisposition.IN_DOUBT
                        detail = str(exc)
                    settlement = MediaPublicationSettlement(plan, disposition, detail)
                    result["publication"] = settlement.to_payload()
                    if disposition is MediaPublicationDisposition.COMMITTED:
                        result["local_path"] = plan.resolved_target
                    else:
                        result["status"] = disposition.value
                        result["materialization_error"] = detail
                return result
            except Exception as exc:  # noqa: BLE001 - keep sibling assets
                return {
                    "status": "failed",
                    "filename": filename,
                    "error": str(exc),
                }

        results = await asyncio.gather(*(generate_one(index, item) for index, item in enumerate(items)))
        successes = [item for item in results if item.get("status") == "success"]
        failed = [item for item in results if item.get("status") != "success"]
        if not successes:
            detail = "; ".join(f"{item.get('filename', '?')}: {item.get('error', 'unknown error')}" for item in failed)
            raise RuntimeError(f"All {plural} failed to generate: {detail}")
        return {
            "summary": f"{len(successes)}/{len(results)} {plural} generated.",
            "results": results,
            "failed": [
                {
                    "filename": item.get("filename"),
                    "error": item.get("error") or item.get("materialization_error"),
                }
                for item in failed
            ],
        }


def _compact(result: dict) -> dict:
    """Reduce a creator's verbose poll dict to just the core per-asset URLs.

    Keeps the one-line ``summary`` and a flat list of ``{filename, url,
    local_path?}`` for successes, plus any ``{filename, error}`` failures — the
    heavy raw poll payload (task ids, request specs) is dropped.
    """
    assets = []
    for r in result.get("results", []):
        if r.get("status") != "success":
            continue
        entry = {
            "filename": r.get("filename"),
            "url": r.get("url") or (r.get("urls") or [""])[0],
        }
        if r.get("local_path"):
            entry["local_path"] = r["local_path"]
        if r.get("materialization_error"):
            entry["materialization_error"] = r["materialization_error"]
        if r.get("publication"):
            entry["publication"] = r["publication"]
        assets.append(entry)
    compact: dict[str, Any] = {"summary": result.get("summary", ""), "assets": assets}
    failed = result.get("failed") or []
    if failed:
        compact["failed"] = failed
    return compact


async def _download(result: dict[str, Any]) -> bytes:
    url = result.get("url") or next(iter(result.get("urls") or []), "")
    if not url:
        raise ValueError("media result has no downloadable URL")
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession() as session:
        async with session.get(str(url), timeout=timeout) as response:
            response.raise_for_status()
            chunks = [chunk async for chunk in response.content.iter_chunked(65536)]
    return b"".join(chunks)


def _default_filename(kind: str) -> str:
    return {
        "image": "image.png",
        "audio": "audio.mp3",
        "music": "music.wav",
        "video": "video.mp4",
    }[kind]


def _publication_transaction_id(invocation_id: str, item_id: str) -> str:
    digest = hashlib.sha256(
        f"mote.generate-media-publication/v1\0{invocation_id}\0{item_id}".encode("utf-8")
    ).hexdigest()
    return f"generate-media-{digest}"


def _generation_operation_key(
    kind: str,
    index: int,
    plan: MediaTargetPlan | None,
) -> str:
    if plan is None:
        return f"{kind}:{index}"
    digest = hashlib.sha256(
        (
            "mote.generate-media-target/v1\0"
            + plan.item_id
            + "\0"
            + plan.requested_target
            + "\0"
            + plan.resolved_target
        ).encode("utf-8")
    ).hexdigest()
    return f"{kind}:{index}:{digest}"
