"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, List, Literal

from mote.contracts.events._base import DurableFact as _DurableFact

if TYPE_CHECKING:
    from mote.contracts.conversation import Message

MESSAGE_APPENDED = "message_appended"

CONTEXT_COMPACTED = "context_compacted"

HISTORY_EDITED = "history_edited"

USER_PROMPT_SUBMIT = "user_prompt_submit"

PROMPT_REJECTED = "prompt_rejected"

POST_COMPACT = "post_compact"


@dataclass
class MessageAppendedEvent:
    """A message was appended to the stored history."""

    message: "Message" = None  # type: ignore[assignment]

    name: ClassVar[str] = MESSAGE_APPENDED


@dataclass
class ContextCompactedEvent:
    """A compaction committed a new model-context projection."""

    model_context_messages: List["Message"] = field(default_factory=list)
    source_message_ids: List[str] = field(default_factory=list)
    summary: str = ""
    strategy: str = ""
    trigger: str = "auto"

    name: ClassVar[str] = CONTEXT_COMPACTED


@dataclass
class HistoryEditedEvent:
    """A user removed messages from the logical transcript and model context."""

    remaining_messages: List["Message"] = field(default_factory=list)
    removed_message_ids: List[str] = field(default_factory=list)
    clear_all: bool = False
    reason: Literal["delete", "clear"] = "delete"

    name: ClassVar[str] = HISTORY_EDITED


@dataclass
class UserPromptSubmitEvent:
    """Safe observation that a user prompt entered this turn."""

    prompt: str = ""

    name: ClassVar[str] = USER_PROMPT_SUBMIT


@dataclass
class PromptRejectedEvent(_DurableFact):
    """Data-minimized audit fact for a prompt denied before admission."""

    prompt_digest: str = ""
    redacted_excerpt: str = ""
    classification: str = ""
    reason: str = ""
    terminate: bool = False

    name: ClassVar[str] = PROMPT_REJECTED
    type: ClassVar[str] = PROMPT_REJECTED

    def __post_init__(self) -> None:
        if not self.prompt_digest.startswith("sha256:"):
            raise ValueError("prompt rejection digest must be a sha256 identity")
        if len(self.redacted_excerpt) > 160:
            raise ValueError("prompt rejection excerpt exceeds 160 characters")
        if not self.classification or not self.reason:
            raise ValueError("prompt rejection classification and reason are required")


@dataclass
class PostCompactEvent:
    """The model-context projection was compacted; summary is optional."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT


MODEL_CONTEXT_REBUILT_EVENTS: tuple[type, ...] = (PostCompactEvent, HistoryEditedEvent)
