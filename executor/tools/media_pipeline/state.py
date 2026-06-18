"""State schema for the media production pipeline graph."""
from __future__ import annotations

from metagpt.executor.tasks.bggraph import GraphState


class MediaPipelineState(GraphState):
    """Input + intermediate state for the multi-media production pipeline.

    Each field is optional (empty list/dict). Nodes detect empty inputs and
    short-circuit with an empty result (skip semantics without graph-level skip).

    Node outputs are stored by the engine as ``state.<node_name> = result``.
    """

    # --- User inputs ---
    prompt: str = ""  # Natural-language request (triggers storyboard planning)
    duration: int = 0  # Target total video duration in seconds (0 = unspecified)
    images: list[dict] = []
    audios: list[dict] = []
    musics: list[dict] = []  # plural plan field; node output lands in extra ``music``
    videos: list[dict] = []
    promo: dict = {}

    # --- Planning ---
    storyboard: dict = {}  # Storyboard LLM output
    promo_dir: str = ""  # Remotion project path

    # --- Intermediate ---
    durations: dict = {}  # {"audio": [...], "music": [...]}
