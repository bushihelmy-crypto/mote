"""State schema for the media production pipeline graph."""
from __future__ import annotations

from typing import Annotated

from mote.executor.tasks.bggraph import GraphState, Output


class MediaPipelineState(GraphState):
    """Input + intermediate state for the multi-media production pipeline.

    Each field is optional (empty list/dict). Nodes detect empty inputs and
    short-circuit with an empty result (skip semantics without graph-level skip).

    State sync is field/channel-based: each node returns a ``{field: value}``
    dict that is merged into the declared fields below. All fields here are
    last-value (no reducers) — no two nodes write the same output field.
    """

    # --- User inputs ---
    prompt: str = ""  # Natural-language request (triggers storyboard planning)
    duration: int = 0  # Target total video duration in seconds (0 = unspecified)
    images: list[dict] = []
    audios: list[dict] = []
    musics: list[dict] = []  # plural plan field; the node writes the ``music`` output
    videos: list[dict] = []
    promo: dict = {}

    # --- Planning ---
    storyboard: dict = {}  # Storyboard LLM output (storyboard node)
    promo_dir: str = ""  # Remotion project path

    # --- Node outputs (merged by field name; downstream nodes read these) ---
    # The produced assets + the final compose are the graph's declared output
    # (returned on success). ``template_init_out`` (dir prep) and
    # ``render_gate_out`` (an internal gate summary) are scaffolding, not
    # deliverables, so they are left unmarked.
    image: Annotated[dict, Output] = {}  # image node output
    audio: Annotated[dict, Output] = {}  # audio node output
    music: Annotated[dict, Output] = {}  # music node output
    video: Annotated[dict, Output] = {}  # video node output
    template_init_out: dict = {}  # template_init node output
    render_gate_out: dict = {}  # render_gate node summary
    promo_out: Annotated[dict, Output] = {}  # promo_render (FFmpeg compose) output

    # --- Intermediate ---
    durations: dict = {}  # {"audio": [...], "music": [...]}
