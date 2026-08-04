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
from typing import TYPE_CHECKING, AsyncContextManager, Optional, Protocol, TypeAlias, TypedDict

from mote.contracts.events.envelope import JsonValue
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.service import HostedServicePayload, HostedServiceResult, ServiceExecutionSemantics
from mote.contracts.task.models import TaskId

if TYPE_CHECKING:
    from mote.contracts.artifact import ArtifactRef
    from mote.contracts.browser import BrowserProfileCommitReceipt, BrowserProfileSnapshot, BrowserStorageState
    from mote.contracts.file import (
        EditCommitOutcome,
        FileByteView,
        FileSnapshot,
        FileTextView,
        MutationResult,
        PdfView,
        ReadRequest,
        SearchOutputMode,
        SearchResult,
    )
    from mote.contracts.interaction import ApprovalChoice, ApprovalRequest, AskUserQuestionAnswers, AskUserQuestionItem
    from mote.contracts.interaction.handoff import HandoffOutcome
    from mote.contracts.output import CommittedOutput
    from mote.contracts.output.graph import GraphOutputContractSpec
    from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
    from mote.contracts.ports.file.operations import GeneratedTargetReservationPort
    from mote.contracts.ports.skill.registry import SkillCatalog
    from mote.runtime.config.device import DeviceConfig
    from mote.runtime.fileops.edit_plans import EditPlan, EditPlanRequest
    from mote.runtime.interactive.host import RuntimeHost
    from mote.runtime.sandbox.runtime import SandboxRuntime
    from mote.runtime.tools.tool_result import ToolResult

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
# Human I/O (available only while an explicit interaction Port is bound)
# ---------------------------------------------------------------------------

AskUser: TypeAlias = Callable[[str], Awaitable[str]]
AskUserQuestion: TypeAlias = Callable[["list[AskUserQuestionItem]"], "Awaitable[AskUserQuestionAnswers]"]
RequestApproval: TypeAlias = Callable[["ApprovalRequest"], "Awaitable[ApprovalChoice]"]
ReplyToUser: TypeAlias = Callable[[str], Awaitable[str]]
EndSession: TypeAlias = Callable[[], Awaitable[str]]

# ---------------------------------------------------------------------------
# Background task pool
# ---------------------------------------------------------------------------

GetBgPool: TypeAlias = Callable[[], BackgroundTaskService]

# ---------------------------------------------------------------------------
# File read-tracking / resource visibility
# ---------------------------------------------------------------------------


class CaptureFileSnapshot(Protocol):
    def __call__(
        self,
        full_path: str,
        *,
        encoding: str | None = None,
        fallback_encoding: str | None = None,
    ) -> "tuple[FileSnapshot, bytes]": ...


ObserveFileSnapshot: TypeAlias = Callable[["FileSnapshot"], None]
ReadFileView: TypeAlias = Callable[
    [str, "ReadRequest"],
    "FileByteView | FileTextView | PdfView",
]


class SearchFiles(Protocol):
    def __call__(
        self,
        *,
        root: str,
        content: str = "",
        files: str = "",
        type_name: str = "",
        output_mode: "SearchOutputMode",
        case_insensitive: bool = False,
        before_context: int = 0,
        after_context: int = 0,
        multiline: bool = False,
        encoding: str | None = None,
        fallback_encoding: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        cursor: str | None = None,
        timeout: float = 20.0,
    ) -> "SearchResult": ...


PlanFileEdit: TypeAlias = Callable[["EditPlanRequest"], "Awaitable[EditPlan]"]
CommitEditPlan: TypeAlias = Callable[[str], "Awaitable[EditCommitOutcome]"]


class CommitGeneratedFiles(Protocol):
    def __call__(
        self,
        files: dict[str, bytes],
        *,
        source: str,
        transaction_id: str | None = None,
    ) -> "Awaitable[MutationResult]": ...


TryReserveGeneratedTargets: TypeAlias = Callable[
    [tuple[str, ...]],
    "GeneratedTargetReservationPort | None",
]
RecordFileGlimpsed: TypeAlias = Callable[[str], None]
IsResourceVisible: TypeAlias = Callable[[str], bool]

# ---------------------------------------------------------------------------
# Browser configuration surface
# ---------------------------------------------------------------------------

GetBrowserStealth: TypeAlias = Callable[[], bool]
GetBrowserLocale: TypeAlias = Callable[[], str]
GetBrowserProxy: TypeAlias = Callable[[], str]
GetBrowserCdpEndpoint: TypeAlias = Callable[[], str]
# Durable browser-login profile: the configured profile name (empty = ephemeral),
# a loader returning a saved ``storage_state`` (or None), and a saver persisting
# one. The store is encrypted at rest (reuses the vault key); the value never
# rides the rollout when a profile is in use.
GetBrowserProfile: TypeAlias = Callable[[], str]
LoadBrowserProfile: TypeAlias = Callable[[str], Optional["BrowserProfileSnapshot"]]
SaveBrowserProfile: TypeAlias = Callable[[str, "BrowserStorageState", Optional[int]], "BrowserProfileCommitReceipt"]
GetBrowserProfileTarget: TypeAlias = Callable[[str], str]
# Client TLS certificates for mutual-TLS logins: each dict is a Playwright
# ``client_certificates`` entry (origin + PEM/PKCS#12 paths + optional
# passphrase, which may still be a secret placeholder the tool expands).
GetBrowserClientCerts: TypeAlias = Callable[[], "list[dict]"]
# CDP endpoint to attach to an already-running Chrome (empty = launch our own).

# ---------------------------------------------------------------------------
# Secret resolution (autonomous login-fill — resolve a secret by KEY, never
# by value). Returns the plaintext for a named secret or ``None`` when the key
# is unknown / secrets are disabled. The value is used transiently inside a tool
# (typed into a page) and never returned to the model or recorded to history.
# ---------------------------------------------------------------------------

GetSecret: TypeAlias = Callable[[str], Optional[str]]

# ---------------------------------------------------------------------------
# Managed interactive runtimes
# ---------------------------------------------------------------------------

GetRuntimeHost: TypeAlias = Callable[[], "RuntimeHost"]


class HandoffRuntime(Protocol):
    def __call__(self, runtime: str, *, message: str = "") -> "Awaitable[HandoffOutcome]": ...


GetArtifactPublisher: TypeAlias = Callable[[], "ReliableArtifactPublisher"]

# ---------------------------------------------------------------------------
# Interruptible sleep
#
# Optional ``duration`` (a durable-timer deadline) → ``Callable[...]``: it may be
# called with no args (indefinite wait) or one (bounded wait).
# ---------------------------------------------------------------------------


class WaitInterruptible(Protocol):
    def __call__(self, duration: float | None = None) -> Awaitable[float]: ...


# ---------------------------------------------------------------------------
# Skills / resources
#
# ``run_skill_fork`` / ``register_resource`` are keyword-only → ``Callable[...]``.
# ---------------------------------------------------------------------------

GetSkillPool: TypeAlias = Callable[[], "Optional[SkillCatalog]"]


class RunSkillFork(Protocol):
    def __call__(
        self,
        *,
        instructions: str,
        arguments: str = "",
        allowed_tools: list[str] | None = None,
        model: str = "",
        effort: str = "",
    ) -> Awaitable[str]: ...


class RegisterResource(Protocol):
    def __call__(self, *, id: str, kind: str, content: str) -> None: ...


RegisterTaskResult: TypeAlias = Callable[[TaskId, str], None]

# ---------------------------------------------------------------------------
# OS-level sandbox runtime (command-execution tools)
# ---------------------------------------------------------------------------

GetSandboxRuntime: TypeAlias = Callable[[], "Optional[SandboxRuntime]"]

# ---------------------------------------------------------------------------
# Device backend configuration (DeviceUse tool)
# ---------------------------------------------------------------------------

GetDeviceConfig: TypeAlias = Callable[[], "DeviceConfig"]

# ---------------------------------------------------------------------------
# Nested tool dispatch (run_graph orchestrator)
# ---------------------------------------------------------------------------

DispatchTool: TypeAlias = Callable[[str, Optional[dict]], "Awaitable[ToolResult]"]
ListToolNames: TypeAlias = Callable[[], list[str]]
ListGraphToolNames: TypeAlias = Callable[[], list[str]]
ListGraphExcludedToolNames: TypeAlias = Callable[[], list[str]]


class CommitGraphOutput(Protocol):
    def __call__(
        self,
        *,
        output: JsonValue,
        contract_spec: "GraphOutputContractSpec",
        run_id: str,
    ) -> "Awaitable[CommittedOutput[JsonValue]]": ...


class ResumeGraphOutput(Protocol):
    def __call__(
        self,
        *,
        contract_spec: "GraphOutputContractSpec",
        run_id: str,
    ) -> "Awaitable[CommittedOutput[JsonValue] | None]": ...


HasGraphOutputRestore: TypeAlias = Callable[[str], bool]

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
# Vision fallback (WebBrowser's ``read_image`` action)
#
# ``describe_image`` is keyword-only past ``image_b64`` (prompt) →
# ``Callable[..., Awaitable[str]]``; the return type still welds and the call
# site's kwargs are checked against the real Role method at the dict literal.
# ---------------------------------------------------------------------------


class DescribeImage(Protocol):
    def __call__(self, __artifact: "ArtifactRef", *, prompt: str = "") -> "Awaitable[str]": ...


InvokeService: TypeAlias = Callable[
    [HostedServicePayload, str, ServiceExecutionSemantics],
    Awaitable[HostedServiceResult],
]
GraphRunLease: TypeAlias = Callable[[str], AsyncContextManager[None]]


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
    capture_file_snapshot: CaptureFileSnapshot
    observe_file_snapshot: ObserveFileSnapshot
    read_file_view: ReadFileView
    search_files: SearchFiles
    plan_file_edit: PlanFileEdit
    commit_edit_plan: CommitEditPlan
    commit_generated_files: CommitGeneratedFiles
    try_reserve_generated_targets: TryReserveGeneratedTargets
    record_file_glimpsed: RecordFileGlimpsed
    is_resource_visible: IsResourceVisible
    get_browser_stealth: GetBrowserStealth
    get_browser_locale: GetBrowserLocale
    get_browser_proxy: GetBrowserProxy
    get_browser_cdp_endpoint: GetBrowserCdpEndpoint
    get_browser_profile: GetBrowserProfile
    load_browser_profile: LoadBrowserProfile
    save_browser_profile: SaveBrowserProfile
    get_browser_profile_target: GetBrowserProfileTarget
    get_browser_client_certs: GetBrowserClientCerts
    get_secret: GetSecret
    get_runtime_host: GetRuntimeHost
    get_artifact_publisher: GetArtifactPublisher
    handoff_runtime: HandoffRuntime
    wait_interruptible: WaitInterruptible
    get_skill_pool: GetSkillPool
    run_skill_fork: RunSkillFork
    register_resource: RegisterResource
    register_task_result: RegisterTaskResult
    get_sandbox_runtime: GetSandboxRuntime
    get_device_config: GetDeviceConfig
    dispatch_tool: DispatchTool
    list_tool_names: ListToolNames
    list_graph_tool_names: ListGraphToolNames
    list_graph_excluded_tool_names: ListGraphExcludedToolNames
    commit_graph_output: CommitGraphOutput
    resume_graph_output: ResumeGraphOutput
    has_graph_output_restore: HasGraphOutputRestore
    graph_run_lease: GraphRunLease
    list_deferred_tools: ListDeferredTools
    reveal_tools: RevealTools
    describe_deferred_tools: DescribeDeferredTools
    describe_image: DescribeImage
    invoke_service: InvokeService
