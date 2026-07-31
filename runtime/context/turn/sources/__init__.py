"""Built-in ephemeral context sources using narrow injected ports.

Sources needing a higher layer (e.g. background-task progress, which imports
``tasks``) live in that layer and implement the same ``EphemeralContextSource``
Protocol; they are injected by ``Role`` rather than re-exported here.
"""

from mote.runtime.context.turn.sources.changed_files import ChangedFilesContextSource
from mote.runtime.context.turn.sources.compaction import CompactionNoticeContextSource
from mote.runtime.context.turn.sources.credential_index import CredentialIndexContextSource
from mote.runtime.context.turn.sources.deferred_tool_index import DeferredToolIndexContextSource
from mote.runtime.context.turn.sources.fold_pressure import FoldPressureContextSource
from mote.runtime.context.turn.sources.git import GitContextSource
from mote.runtime.context.turn.sources.skill_activation import SkillActivationContextSource
from mote.runtime.context.turn.sources.skill_listing import SkillListingContextSource
from mote.runtime.context.turn.sources.split_tool_menu import SplitToolMenuContextSource
from mote.runtime.context.turn.sources.team import TeamContextSource
from mote.runtime.context.turn.sources.timestamp import TimestampContextSource
from mote.runtime.context.turn.sources.token_pressure import TokenPressureContextSource
from mote.runtime.context.turn.sources.tool_catalog import ToolCatalogContextSource
from mote.runtime.context.turn.sources.toolset_instructions import ToolsetInstructionsContextSource

__all__ = [
    "ChangedFilesContextSource",
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
    "ToolsetInstructionsContextSource",
]
