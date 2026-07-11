"""Build the media production pipeline as a BgGraph.

Topology::

    START → storyboard ─conditional→ {
        full_pipeline: template_init,
        assets_only:   dispatch
    }

    template_init → dispatch

    dispatch ──┬── image ──┬── video ──────────────┐
               ├── audio ──┤                       │
               └── music ──┘                       │
                                                   ├── render_gate ─conditional→ {
               [audio, music] → duration_measure ──┘       render: promo_render → END,
                                                           done:   END
                                                   }

- storyboard: LLM planning or pass-through (ONLY conditional edges out)
- template_init: resolve + prepare the output directory (full_pipeline path only)
- dispatch: no-op fan-out to parallel media generation nodes
- image/audio/music: run in parallel
- video: waits for image (may reference image outputs as first_frame)
- duration_measure: AND-join on audio + music → measure timings
- render_gate: AND-join on video + duration_measure → route decision
- promo_render: final FFmpeg compose (clips + narration + music) → END
"""
from __future__ import annotations

from mote.executor.tasks.bggraph import END, START, BgGraph

from .nodes import (
    _route_after_render_gate,
    _route_after_storyboard,
    audio_node,
    dispatch_node,
    duration_measure_node,
    image_node,
    music_node,
    promo_node,
    render_gate_node,
    storyboard_node,
    template_init_node,
    video_node,
)
from .state import MediaPipelineState


def build_media_pipeline_graph() -> BgGraph:
    """Construct and return the media pipeline graph (not yet compiled)."""
    g = BgGraph("media_pipeline", state_schema=MediaPipelineState, recursion_limit=20)

    # --- Nodes ---
    g.add_node("storyboard", storyboard_node)
    g.add_node("template_init", template_init_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("image", image_node)
    g.add_node("audio", audio_node)
    g.add_node("music", music_node)
    g.add_node("video", video_node)
    g.add_node("duration_measure", duration_measure_node)
    g.add_node("render_gate", render_gate_node)
    g.add_node("promo_render", promo_node)

    # --- START → storyboard (single entry) ---
    g.add_edge(START, "storyboard")

    # --- Conditional: storyboard routes to full_pipeline or assets_only ---
    g.add_conditional_edges(
        "storyboard",
        _route_after_storyboard,  # type: ignore[arg-type]  # router reads MediaPipelineState (a GraphState subclass)
        {"full_pipeline": "template_init", "assets_only": "dispatch"},
    )

    # --- template_init → dispatch (paths converge) ---
    g.add_edge("template_init", "dispatch")

    # --- dispatch fans out to the three parallel tracks ---
    g.add_edge("dispatch", "image")
    g.add_edge("dispatch", "audio")
    g.add_edge("dispatch", "music")

    # --- image → video (video may reference image outputs as first_frame) ---
    g.add_edge("image", "video")

    # --- AND-join: audio + music → duration_measure ---
    g.add_edge(["audio", "music"], "duration_measure")

    # --- AND-join: video + duration_measure → render_gate ---
    g.add_edge(["video", "duration_measure"], "render_gate")

    # --- Conditional: render_gate routes to render or done ---
    g.add_conditional_edges(
        "render_gate",
        _route_after_render_gate,  # type: ignore[arg-type]  # router reads MediaPipelineState (a GraphState subclass)
        {"render": "promo_render", "done": END},
    )

    # --- promo_render → END ---
    g.add_edge("promo_render", END)

    return g
