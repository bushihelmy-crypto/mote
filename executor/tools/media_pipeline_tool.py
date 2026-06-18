"""MediaPipeline — multi-media production orchestration tool.

Generates images, audio (TTS), music, video, and renders a final promo video in
one background task. The agent passes a natural-language prompt describing what
to produce, and the pipeline's storyboard LLM plans all media assets automatically.

Nodes run in parallel where possible; video waits for images, duration_measure
waits for audio+music, render_gate joins video+duration_measure before deciding
whether to render. Empty inputs skip the corresponding node.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tasks.types import BgTaskResult


class PromoParams(BaseModel):
    """Remotion render parameters."""

    promo_dir: str
    width: int
    height: int
    source_orientation: Optional[str] = None


@register_tool
class MediaPipeline(BaseTool):
    name = "MediaPipeline"
    aliases = ["media_pipeline", "generate_media"]
    description = (
        "One-stop multimedia production pipeline. Use this tool whenever the user "
        "asks to make a video, generate media, or produce visual/audio content.\n\n"
        "IMPORTANT: You MUST always pass both `prompt` and `promo`.\n\n"
        "Parameters:\n"
        "  - prompt: (REQUIRED) natural-language description of what to produce. "
        "Include all details the user mentioned (duration, style, language, content).\n"
        "  - promo: (REQUIRED) {promo_dir, width, height, source_orientation?} — "
        "output directory and settings. promo_dir is where assets are saved and the "
        "final video is composed. If the user does not specify resolution, "
        "use width=1920 and height=1080. If the user does not specify a path, "
        "use the current working directory.\n\n"
        "Behavior:\n"
        "  - The pipeline automatically plans what images, audio, music, and video "
        "clips to generate based on the prompt.\n"
        "  - Images, audio, and music generate in parallel.\n"
        "  - Video waits for images (can use them as first frames).\n"
        "  - The final video is composed automatically with FFmpeg: clips are "
        "concatenated in order, narration is placed at each clip's start offset, "
        "background music is ducked under the narration, and audio is trimmed to "
        "the video length. Output: promo_dir/promo-{width}x{height}.mp4."
    )

    def __init__(self):
        super().__init__()
        from metagpt.executor.tools.media_pipeline.graph import build_media_pipeline_graph

        self._graph = build_media_pipeline_graph()
        self._executor = self._graph.compile()

    async def call(
        self,
        *,
        prompt: str,
        promo: PromoParams | dict,
        duration: int = 0,
    ) -> BgTaskResult:
        """Run the media production pipeline.

        Args:
            prompt: Natural-language description of what to produce. The storyboard
                node uses LLM to plan all media assets (images, audio, music, video)
                automatically from this prompt.
            promo: Render settings object: {promo_dir, width, height,
                source_orientation?}. promo_dir is where assets are saved and the
                final video is rendered.
            duration: Target total video duration in seconds (0 = unspecified).
                Threaded into the storyboard node so asset timing sums to it.
        """
        if isinstance(promo, dict):
            promo_dict = promo
        else:
            promo_dict = promo.model_dump(exclude_none=True)
        return await self._executor(
            prompt=prompt,
            duration=duration,
            images=[],
            audios=[],
            musics=[],
            videos=[],
            promo=promo_dict,
            promo_dir=promo_dict.get("promo_dir", ""),
        )
