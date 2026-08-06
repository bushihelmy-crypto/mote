"""Conversation message routing and metadata field names."""

MESSAGE_ROUTE_FROM = "sent_from"
MESSAGE_ROUTE_TO = "send_to"
MESSAGE_ROUTE_CAUSE_BY = "cause_by"
MESSAGE_META_ROLE = "role"
MESSAGE_ROUTE_TO_ALL = "<all>"
MESSAGE_ROUTE_TO_NONE = "<none>"
MESSAGE_ROUTE_TO_SELF = "<self>"  # Add this tag to replace `ActionOutput`

# Metadata defines
AGENT = "agent"
IMAGES = "images"
PDFS = "pdfs"
# Native tool-use metadata keys (carried in Message.metadata, never in content).
# TOOL_CALLS: list of {id, name, args} on an assistant message that invoked tools.
# TOOL_CALL_ID: the call id on a tool-result message (role="tool").
TOOL_CALLS = "tool_calls"
TOOL_CALL_ID = "tool_call_id"
TOOL_EFFECT_RECEIPT_ID = "tool_effect_receipt_id"
TOOL_EFFECT_PRESENTATION_DIGEST = "tool_effect_presentation_digest"
# TOOL_REFERENCES: names of tools the model just discovered via SearchTools, on a
# tool-result message. On the Anthropic native wire (server-side tool-search,
# "custom" path) the tool_result's content is rendered as a list of
# ``tool_reference`` blocks — one per name — instead of the human-readable text,
# and the API expands each into the tool's full definition. Metadata-as-truth:
# survives dump/load like the other tool keys; every other provider ignores it.
TOOL_REFERENCES = "tool_references"

# Resource-projection metadata keys (carried in Message.metadata).
# A "resource" is a dynamically-loaded capability body (e.g. a Skill body) that
# must survive history compaction. These keys are the TRUTH that outlives replay:
# Message.load reconstructs via the base Message.from_dict (cls(**m)), so the
# ResourceMessage subclass identity is lost, but metadata is preserved. Every
# consumer (compaction skip, post-compact re-projection, resume rebuild) keys off
# these, never off isinstance(msg, ResourceMessage).
# RESOURCE_ID: stable id of the loaded resource (e.g. the skill name).
# RESOURCE_KIND: resource category ("skill", ...), for future multi-kind support.
# RESOURCE_STICKY: True => re-project this body after compaction / never fold it.
RESOURCE_ID = "resource_id"
RESOURCE_KIND = "resource_kind"
RESOURCE_STICKY = "resource_sticky"

# Resource provenance of a tool-result (carried in Message.metadata on a
# tool-result message). Records which durable resource — today a filesystem
# path — a *reconstructable* result was derived from, so the visibility layer
# can answer "is this file's last read still present in context?" without the
# caller having to know the opaque tool_call_id. Stamped by the channel from
# ``ToolResult.resource_path``; read by :class:`ContextVisibility`. Absent on
# results that are not tied to a re-readable resource. Like the other keys here,
# it is metadata-as-truth: it survives dump/load even though the ToolMessage
# subclass identity does not.
TOOL_RESULT_RESOURCE_PATH = "tool_result_resource_path"

# Result-lifecycle retention (carried in Message.metadata on a tool-result message).
# This is the model-facing counterpart to the tool author's static ``reconstructable``
# ClassVar: a per-result hint about how compaction should treat *this specific*
# tool_result. The channel only carries the string; the compaction layer is the
# sole interpreter. Unknown / absent values fall back to default behavior.
#   RETENTION_ERASABLE — this result may be dropped early even if its tool is not
#     statically reconstructable (the model has read it and no longer needs it).
#   RETENTION_PIN       — never fold, never drop this result during compaction.
#   RETENTION_DEFAULT   — no hint; behave as today (drop iff reconstructable).
RETENTION = "retention"
RETENTION_ERASABLE = "erasable"
RETENTION_PIN = "pin"
RETENTION_DEFAULT = "default"

# Prompt-cache intent (carried in Message.metadata; declarative, provider-agnostic).
# The message declares its *caching semantics*; each provider translates the intent
# into its own mechanism (Anthropic cache_control breakpoints / OpenAI automatic
# prefix caching). Upper layers never touch cache_control; providers never guess
# "which message is the volatile tail" from position.
#   CACHE_INTENT_DURABLE        — default: may serve as a stable, cacheable prefix.
#   CACHE_INTENT_EPHEMERAL_TAIL — a per-turn re-synthesized tail (the command +
#     <system-reminder> prompt) that is NOT stored in history and reappears each
#     turn with different bytes; it must never anchor a cache breakpoint, or the
#     next turn's prefix loses its anchor and the whole history re-writes.
CACHE_INTENT = "cache_intent"
CACHE_INTENT_DURABLE = "durable"
CACHE_INTENT_EPHEMERAL_TAIL = "ephemeral_tail"

# Interjection framing (carried in Message.metadata on a user message).
# INTERJECTION: True => this user message arrived *mid-turn* (the agent was
# already working) and has been wrapped with framing so the model can tell it
# apart from the turn's original prompt. The flag makes the wrap idempotent (a
# message is never double-framed) and lets consumers identify a steering
# message. Metadata-as-truth: survives dump/load into the durable rollout, so a
# resumed session still reads the message as the interjection it was.
INTERJECTION = "interjection"

# Message id
IGNORED_MESSAGE_ID = "0"
