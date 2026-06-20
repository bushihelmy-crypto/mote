"""Built-in ephemeral context sources that depend only on ``common`` / ducks.

Sources needing a higher layer (e.g. background-task progress, which imports
``tasks``) live in that layer and implement the same ``EphemeralContextSource``
Protocol; they are injected by ``Role`` rather than re-exported here.
"""

from metagpt.context.turn_context.sources.compaction import (
    CompactionNoticeContextSource,
)
from metagpt.context.turn_context.sources.git import GitContextSource
from metagpt.context.turn_context.sources.skill_activation import (
    SkillActivationContextSource,
)
from metagpt.context.turn_context.sources.token_pressure import (
    TokenPressureContextSource,
)

__all__ = [
    "CompactionNoticeContextSource",
    "GitContextSource",
    "SkillActivationContextSource",
    "TokenPressureContextSource",
]
