"""Pure names describing one session footprint beneath an injected root."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionLayout:
    sessions_dir: str = ".agent_sessions"
    rollout_file: str = "rollout.jsonl"
    default_session: str = "default"


__all__ = ["SessionLayout"]
