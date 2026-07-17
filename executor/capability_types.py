"""Typed contract for the Role→tool capability injection seam.

A tool declares the capabilities it needs by name (``BaseTool.requires``);
``bind()`` resolves each name against :meth:`Role.tool_capabilities` (the
explicit allowlist) and ``setattr``\\ s the bound method onto the tool. That
crosses two boundaries with nothing statically linking them: the publisher hands
back bound methods, and each consumer hand-writes a ``Callable[...]`` annotation
for the attribute it expects to receive.

This module is the single source of truth that welds the two ends together:

* one named ``Callable`` alias per capability, matching the Role publisher's
  signature — so the consumer imports the alias instead of re-typing it, and
  pyright flags any drift between what a tool expects and what the Role exposes;
* a :class:`CapabilityMap` ``TypedDict`` keyed by capability name — the declared
  return type of :meth:`Role.tool_capabilities`, so pyright checks every bound
  method in that dict literal against its declared signature (a renamed/retyped
  Role method fails the build, not a runtime ``bind()``).

**Layering:** this lives in the ``executor`` layer on purpose. The executor is a
leaf with respect to ``roles`` (dependency flows roles→executor, never back), so
both ``roles.role`` (publisher) and ``executor.tools.*`` (consumers) can import
these aliases without a cycle.

The residual gap the weld does *not* close: the ``requires`` string itself is
still a plain literal, so a typo in a tool's ``requires`` (a name absent from the
map) stays a runtime ``bind()`` failure rather than a type error. Closing that
last seam would require eliminating the runtime string indirection, which is
exactly the capability-isolation seam we keep on purpose — so it stays runtime.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional, TypeAlias, TypedDict

if TYPE_CHECKING:
    from mote.common.schema import AskUserQuestionAnswers, AskUserQuestionItem
    from mote.common.schema.permission_types import ApprovalChoice, ApprovalRequest
    from mote.context.skills.skill_pool import SkillPool
    from mote.executor.tasks import BackgroundTaskPool
    from mote.executor.tool_result import ToolResult
    from mote.router.llm.llm_response import WebSearchHit
    from mote.sandbox import SandboxRuntime

# ---------------------------------------------------------------------------
# Working directory / lifecycle
# ---------------------------------------------------------------------------

GetCwd: TypeAlias = Callable[[], str]
SetCwd: TypeAlias = Callable[[str], None]
Deactivate: TypeAlias = Callable[[], None]
# The name of the default (main think-loop) model — used by media tools (Read /
# WebBrowser screenshot) to check ``supports_vision`` / ``supports_pdf_input``
# up-front and refuse with a ToolNotConfiguredError, rather than attaching media
# the model silently cannot read. ``None`` when no default model is configured.
GetDefaultModel: TypeAlias = Callable[[], Optional[str]]

# ---------------------------------------------------------------------------
# Human I/O (only valid inside a MoteEnv)
# ---------------------------------------------------------------------------

AskUser: TypeAlias = Callable[[str], Awaitable[str]]
AskUserQuestion: TypeAlias = Callable[["list[AskUserQuestionItem]"], "Awaitable[AskUserQuestionAnswers]"]
RequestApproval: TypeAlias = Callable[["ApprovalRequest"], "Awaitable[ApprovalChoice]"]
ReplyToUser: TypeAlias = Callable[[str], Awaitable[str]]
EndSession: TypeAlias = Callable[[], Awaitable[str]]

# ---------------------------------------------------------------------------
# Background task pool
# ---------------------------------------------------------------------------

GetBgPool: TypeAlias = Callable[[], "BackgroundTaskPool"]

# ---------------------------------------------------------------------------
# File read-tracking / resource visibility
# ---------------------------------------------------------------------------

RecordFileRead: TypeAlias = Callable[[str, int], None]
GetFileReadMtime: TypeAlias = Callable[[str], Optional[int]]
RecordFileGlimpsed: TypeAlias = Callable[[str], None]
IsResourceVisible: TypeAlias = Callable[[str], bool]

# ---------------------------------------------------------------------------
# Durable state recorders / pending-restore accessors
#
# The recorders take a keyword-only ``tool`` (and, for browser, ``active`` /
# ``storage_state``); ``Callable`` cannot express keyword-only params, so these
# use ``Callable[..., None]`` — the return type still welds, and the call sites
# pass kwargs pyright checks against the real Role method at the dict literal.
# ---------------------------------------------------------------------------

RecordFileSnapshot: TypeAlias = Callable[..., None]
RecordTerminalState: TypeAlias = Callable[..., None]
TakePendingTerminalRestore: TypeAlias = Callable[[], Optional[dict]]
RecordKernelState: TypeAlias = Callable[..., None]
TakePendingKernelRestore: TypeAlias = Callable[[], Optional[dict]]
RecordBrowserState: TypeAlias = Callable[..., None]
TakePendingBrowserRestore: TypeAlias = Callable[[], Optional[dict]]

# ---------------------------------------------------------------------------
# Browser configuration surface
# ---------------------------------------------------------------------------

GetBrowserHeadless: TypeAlias = Callable[[], bool]
GetBrowserStealth: TypeAlias = Callable[[], bool]
GetBrowserLocale: TypeAlias = Callable[[], str]
GetBrowserProxy: TypeAlias = Callable[[], str]

# ---------------------------------------------------------------------------
# Stateful-tool session slots (terminal shell / python kernel)
# ---------------------------------------------------------------------------

GetToolSession: TypeAlias = Callable[[str], Any]
SetToolSession: TypeAlias = Callable[[str, Any], None]

# ---------------------------------------------------------------------------
# Interruptible sleep
# ---------------------------------------------------------------------------

WaitInterruptible: TypeAlias = Callable[[], Awaitable[float]]

# ---------------------------------------------------------------------------
# Skills / resources
#
# ``run_skill_fork`` / ``register_resource`` are keyword-only → ``Callable[...]``.
# ---------------------------------------------------------------------------

GetSkillPool: TypeAlias = Callable[[], "Optional[SkillPool]"]
RunSkillFork: TypeAlias = Callable[..., Awaitable[str]]
RegisterResource: TypeAlias = Callable[..., None]
RegisterTaskResult: TypeAlias = Callable[[str, str], None]
RetireTaskResult: TypeAlias = Callable[[str], None]

# ---------------------------------------------------------------------------
# OS-level sandbox runtime (command-execution tools)
# ---------------------------------------------------------------------------

GetSandboxRuntime: TypeAlias = Callable[[], "Optional[SandboxRuntime]"]

# ---------------------------------------------------------------------------
# Nested tool dispatch (run_graph orchestrator)
# ---------------------------------------------------------------------------

DispatchTool: TypeAlias = Callable[[str, Optional[dict]], "Awaitable[ToolResult]"]
ListToolNames: TypeAlias = Callable[[], list[str]]
ListGraphToolNames: TypeAlias = Callable[[], list[str]]
ListGraphExcludedToolNames: TypeAlias = Callable[[], list[str]]

# ---------------------------------------------------------------------------
# Tool search (deferred-tool discovery — SearchTools meta-tool)
# ---------------------------------------------------------------------------

ListDeferredTools: TypeAlias = Callable[[], dict[str, str]]
RevealTools: TypeAlias = Callable[[list[str]], list[str]]
# Resolve the FULL (multi-line) descriptions of named deferred tools — the prose
# SearchTools persists into the ResourceRegistry on reveal (so it enters cached
# history + survives compaction), rather than re-sending it on the reminder tail.
DescribeDeferredTools: TypeAlias = Callable[[list[str]], dict[str, str]]

# ---------------------------------------------------------------------------
# Server-side web search (WebSearch tool's secondary call)
#
# ``web_search`` is keyword-only past ``query`` (allowed_domains/blocked_domains/
# max_uses) → ``Callable[..., Awaitable[...]]``; the return type still welds and
# the call site's kwargs are checked against the real Role method at the dict
# literal.
# ---------------------------------------------------------------------------

WebSearch: TypeAlias = Callable[..., "Awaitable[list[WebSearchHit]]"]

# ---------------------------------------------------------------------------
# Vision fallback (WebBrowser's ``read_image`` action)
#
# ``describe_image`` is keyword-only past ``image_b64`` (prompt) →
# ``Callable[..., Awaitable[str]]``; the return type still welds and the call
# site's kwargs are checked against the real Role method at the dict literal.
# ---------------------------------------------------------------------------

DescribeImage: TypeAlias = Callable[..., "Awaitable[str]"]


class CapabilityMap(TypedDict):
    """The full set of capabilities :meth:`Role.tool_capabilities` publishes.

    Keys are exactly the names a tool may list in ``requires``; values are the
    bound methods, typed by the aliases above. Declaring this as the return type
    of ``tool_capabilities`` welds the publisher: pyright checks each entry in
    the returned dict literal against its declared signature, so renaming or
    retyping a Role capability method (without updating the alias) fails the
    build instead of surfacing as a runtime ``bind()`` error.
    """

    get_cwd: GetCwd
    set_cwd: SetCwd
    get_default_model: GetDefaultModel
    deactivate: Deactivate
    ask_user: AskUser
    ask_user_question: AskUserQuestion
    get_bg_pool: GetBgPool
    request_approval: RequestApproval
    reply_to_user: ReplyToUser
    end_session: EndSession
    record_file_read: RecordFileRead
    get_file_read_mtime: GetFileReadMtime
    record_file_glimpsed: RecordFileGlimpsed
    is_resource_visible: IsResourceVisible
    record_file_snapshot: RecordFileSnapshot
    record_terminal_state: RecordTerminalState
    take_pending_terminal_restore: TakePendingTerminalRestore
    record_kernel_state: RecordKernelState
    take_pending_kernel_restore: TakePendingKernelRestore
    record_browser_state: RecordBrowserState
    take_pending_browser_restore: TakePendingBrowserRestore
    get_browser_headless: GetBrowserHeadless
    get_browser_stealth: GetBrowserStealth
    get_browser_locale: GetBrowserLocale
    get_browser_proxy: GetBrowserProxy
    get_tool_session: GetToolSession
    set_tool_session: SetToolSession
    wait_interruptible: WaitInterruptible
    get_skill_pool: GetSkillPool
    run_skill_fork: RunSkillFork
    register_resource: RegisterResource
    register_task_result: RegisterTaskResult
    retire_task_result: RetireTaskResult
    get_sandbox_runtime: GetSandboxRuntime
    dispatch_tool: DispatchTool
    list_tool_names: ListToolNames
    list_graph_tool_names: ListGraphToolNames
    list_graph_excluded_tool_names: ListGraphExcludedToolNames
    list_deferred_tools: ListDeferredTools
    reveal_tools: RevealTools
    describe_deferred_tools: DescribeDeferredTools
    web_search: WebSearch
    describe_image: DescribeImage
