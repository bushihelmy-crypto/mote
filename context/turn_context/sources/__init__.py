"""Built-in ephemeral context sources that depend only on ``common`` / ducks.

Sources needing a higher layer (e.g. background-task progress, which imports
``tasks``) live in that layer and implement the same ``EphemeralContextSource``
Protocol; they are injected by ``Role`` rather than re-exported here.
"""

from mote.context.turn_context.sources.changed_files import ChangedFilesContextSource
from mote.context.turn_context.sources.code_map import CodeMapContextSource
from mote.context.turn_context.sources.compaction import CompactionNoticeContextSource
from mote.context.turn_context.sources.credential_index import CredentialIndexContextSource
from mote.context.turn_context.sources.deferred_tool_index import DeferredToolIndexContextSource
from mote.context.turn_context.sources.fold_pressure import FoldPressureContextSource
from mote.context.turn_context.sources.git import GitContextSource
from mote.context.turn_context.sources.skill_activation import SkillActivationContextSource
from mote.context.turn_context.sources.skill_listing import SkillListingContextSource
from mote.context.turn_context.sources.split_tool_menu import SplitToolMenuContextSource
from mote.context.turn_context.sources.team import TeamContextSource
from mote.context.turn_context.sources.timestamp import TimestampContextSource
from mote.context.turn_context.sources.token_pressure import TokenPressureContextSource
from mote.context.turn_context.sources.tool_catalog import ToolCatalogContextSource

__all__ = [
    "ChangedFilesContextSource",
    "CodeMapContextSource",
    "CompactionNoticeContextSource",
    "CredentialIndexContextSource",
    "DeferredToolIndexContextSource",
    "FoldPressureContextSource",
    "GitContextSource",
    "SkillActivationContextSource",
    "SkillListingContextSource",
    "SplitToolMenuContextSource",
    "TeamContextSource",
    "TimestampContextSource",
    "TokenPressureContextSource",
    "ToolCatalogContextSource",
]
