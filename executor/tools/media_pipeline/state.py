"""State schema for the media production pipeline graph."""
from __future__ import annotations

from mote.executor.tasks.bggraph import GraphState


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
    image: dict = {}  # image node output
    audio: dict = {}  # audio node output
    music: dict = {}  # music node output
    video: dict = {}  # video node output
    template_init_out: dict = {}  # template_init node output
    render_gate_out: dict = {}  # render_gate node summary
    promo_out: dict = {}  # promo_render (FFmpeg compose) output

    # --- Intermediate ---
    durations: dict = {}  # {"audio": [...], "music": [...]}
