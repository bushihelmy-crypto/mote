"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, List, Literal, cast

from mote.contracts.events._base import DurableFact as _DurableFact
from mote.contracts.events.envelope import JsonValue

if TYPE_CHECKING:
    from mote.contracts.conversation import Message

MESSAGE_APPENDED = "message_appended"

CONTEXT_COMPACTED = "context_compacted"

HISTORY_EDITED = "history_edited"

USER_PROMPT_SUBMIT = "user_prompt_submit"

PROMPT_REJECTED = "prompt_rejected"

POST_COMPACT = "post_compact"

TURN_CONTEXT_COLLECTED = "turn_context_collected"


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


@dataclass(frozen=True, slots=True)
class TurnContextCollectedEvent:
    """Request-only turn context collected for the current model invocation.

    This in-process observation lets presentation show the exact ephemeral
    reminder sent to the model without changing its request-only lifecycle.
    """

    content: str

    name: ClassVar[str] = TURN_CONTEXT_COLLECTED


@dataclass(frozen=True)
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

    def payload(self) -> dict[str, object]:
        return {
            "prompt_digest": self.prompt_digest,
            "redacted_excerpt": self.redacted_excerpt,
            "classification": self.classification,
            "reason": self.reason,
            "terminate": self.terminate,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, JsonValue]) -> "PromptRejectedEvent":
        expected = {"prompt_digest", "redacted_excerpt", "classification", "reason", "terminate"}
        if set(payload) != expected:
            raise ValueError(f"{cls.__name__} payload fields are not canonical")
        prompt_digest = payload["prompt_digest"]
        redacted_excerpt = payload["redacted_excerpt"]
        classification = payload["classification"]
        reason = payload["reason"]
        terminate = payload["terminate"]
        if not all(type(value) is str for value in (prompt_digest, redacted_excerpt, classification, reason)):
            raise TypeError(f"{cls.__name__} text fields must be strings")
        if type(terminate) is not bool:
            raise TypeError(f"{cls.__name__}.terminate must be a boolean")
        return cls(
            prompt_digest=cast(str, prompt_digest),
            redacted_excerpt=cast(str, redacted_excerpt),
            classification=cast(str, classification),
            reason=cast(str, reason),
            terminate=terminate,
        )


@dataclass
class PostCompactEvent:
    """The model-context projection was compacted; summary is optional."""

    trigger: str = "auto"
    summary: str = ""

    name: ClassVar[str] = POST_COMPACT


MODEL_CONTEXT_REBUILT_EVENTS: tuple[type, ...] = (PostCompactEvent, HistoryEditedEvent)
ModelContextRebuiltEvent = PostCompactEvent | HistoryEditedEvent
