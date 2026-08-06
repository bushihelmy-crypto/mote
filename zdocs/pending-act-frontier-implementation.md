# PendingAct Frontier 持久恢复实施文档

## 1. 实施原则

本文落实 [`pending-act-frontier-requirements.md`](./pending-act-frontier-requirements.md)。实现必须保持以下边界：

- Approval与Execution独立；
- PendingAct只关联事实，不拥有审批、ExternalEffect或FileOps状态机；
- Session stream是唯一durable事实链；
- Restore读取一个revision-consistent snapshot；
- ActNode、ActionExecutionService、ToolExecutor保持唯一。
- 用户中断先持久化、后触发进程内取消；`<turn_aborted>`仅为模型上下文投影。

## 2. 当前源码与目标改动

### 2.1 当前入口

```text
kernel/execution/graph/react.py::build_react_graph
kernel/execution/graph/nodes.py::RestoreNode/ActNode
kernel/execution/state.py::ExecutionState
kernel/execution/operations/action_execution.py::ActionExecutionService
runtime/tools/tool_pipeline.py::ToolExecutionPipeline
runtime/tools/policy.py::DefaultToolCallPolicy
runtime/tools/permission/engine.py::PermissionEngine
runtime/session/committer.py::SessionFactCommitter
runtime/session/projection.py::SessionLiveProjection
runtime/events/fabric.py::EventFabric
```

### 2.2 当前缺口

1. `RestoreNode`只恢复committed output，固定否则进入OBSERVE。
2. Approval在`PermissionEngine.check*()`内部直接等待，没有durable request ID。
3. `ToolEffectStore`独立写`tool-effects.jsonl`，无法与Session原子。
4. `EventFabric`冲突后会reconcile并按最新version重试，不适用于领域CAS。
5. snapshot binding只在`BoundToolRegistry`内存中。
6. `RuntimeExecutionTransaction`的revision/fence校验是进程内检查。

## 3. 目标包与文件

```text
contracts/execution/pending_act.py
contracts/execution/run_cursor.py
contracts/interaction/approval.py
contracts/tool/external_effect.py
contracts/events/tool.py
contracts/ports/execution/pending_act.py
contracts/ports/events/journal.py
contracts/ports/session/facts.py

runtime/session/pending_act.py
runtime/session/pending_act_claim.py
runtime/session/execution_restore.py
runtime/session/events.py
runtime/session/committer.py
runtime/session/projection.py
runtime/events/fabric.py
runtime/tools/approval.py
runtime/tools/tool_pipeline.py
runtime/tools/snapshots.py
```

已有canonical type存在时直接复用，不创建同义类型。

## 4. Contracts

### 4.1 PendingAct contracts

新增`contracts/execution/pending_act.py`：

```python
@dataclass(frozen=True, slots=True)
class PendingActFrontierId:
    value: str

@dataclass(frozen=True, slots=True)
class PendingActionArgumentsRevision:
    invocation_id: ToolInvocationId
    revision: int
    arguments: ToolArguments
    arguments_digest: str

@dataclass(frozen=True, slots=True)
class PendingAction:
    ordinal: int
    invocation_id: ToolInvocationId
    action_id: str
    tool_name: str
    definition_identity: str
    catalog_generation: int
    effect: ToolEffect
    current_arguments_revision: int
    fileops_transaction_id: FileTransactionId | None

@dataclass(frozen=True, slots=True)
class PendingActFrontier:
    schema_version: Literal[1]
    frontier_id: PendingActFrontierId
    session_id: str
    run_id: str
    model_call_id: str
    revision: int
    definition_ref: ToolCompositionDefinitionRef
    actions: tuple[PendingAction, ...]
```

注意：`PendingAction`没有混合生命周期字段，也不复制Approval request identity。Approval request按frontier/invocation/arguments revision与permission-target digest确定性关联；Approval、ExternalEffect与FileOps状态从各自projection按identity关联。

校验：ordinal连续、identity唯一、revision非负、arguments digest严格、effect与definition一致、final candidate禁止进入。

### 4.2 Approval contracts

修改`contracts/interaction/approval.py`：

```python
class ApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    WAITING = "waiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

@dataclass(frozen=True, slots=True)
class ApprovalRequestId:
    value: str

@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: ApprovalRequestId
    frontier_id: PendingActFrontierId
    invocation_id: ToolInvocationId
    arguments_revision: int
    arguments_digest: str
    permission_targets_digest: str
    expected_frontier_revision: int
    # 现有tool_name/kind/target/paths/risk/reason展示字段

class ApprovalDisposition(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    REJECT = "reject"
    CANCEL = "cancel"
```

Approval decision DTO绑定request ID、disposition、decided arguments和expected revision。

迁移`contracts/ports/interaction/role.py`与Product ports，删除旧`ApprovalChoice` Literal，不留alias。

### 4.3 ExternalEffect contracts

新增`contracts/tool/external_effect.py`：

```python
class ExternalEffectState(StrEnum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_DOUBT = "in_doubt"

@dataclass(frozen=True, slots=True)
class ToolEffectReceipt:
    receipt_id: str
    identity: ToolInvocationIdentity
    disposition: Literal["succeeded", "failed"]
    provider_evidence: JsonValue
    artifacts: tuple[ArtifactRef, ...]
    media: tuple[ToolMedia, ...]
    file_changes: tuple[FileChange, ...]
    presentation_digest: str
```

`IN_DOUBT`只属于ExternalEffectState。ApprovalState不得引用它。

### 4.4 Run cursor

新增`contracts/execution/run_cursor.py`：

```python
@dataclass(frozen=True, slots=True)
class RunRecoveryCursor:
    run_id: str
    revision: int
    next_node: Literal[NodeId.ACT, NodeId.OBSERVE]
    pending_act_id: PendingActFrontierId | None
    continue_inference: bool
```

若Contracts不能import Kernel `NodeId`，在Contracts定义窄`RecoveryTarget.ACT/OBSERVE` enum，Kernel显式映射；不得用裸字符串。

### 4.5 Source facts

扩展`contracts/events/tool.py`：

```text
PendingActSchemaActivatedEvent
RunExecutionStartedEvent
PendingActCreatedEvent
PendingActionArgumentsRevisedEvent
ApprovalRequestedEvent
ApprovalDecisionCommittedEvent
ExternalEffectStartedEvent
ExternalEffectFinishedEvent
ExternalEffectInDoubtEvent
PendingActionResultCommittedEvent
PendingActionsSkippedEvent
PendingActSettledEvent
RunRecoveryCursorAdvancedEvent
PendingActClaimAcquiredEvent
PendingActClaimRenewedEvent
PendingActClaimTakenOverEvent
PendingActClaimReleasedEvent
TurnInterruptedEvent
TurnInterruptSettledEvent
TurnInterruptedContextAttachedEvent
PendingActInterruptedEvent
```

LOCAL不新增状态fact；PendingAct action保存确定性FileOps transaction ID，B引用现有FileOps receipt。

每个fact有严格`payload()/from_payload()`，拒绝额外字段。

## 5. Journal 原子 CAS 与 writer fence

### 5.1 Port

扩展`contracts/ports/events/journal.py`：

```python
@dataclass(frozen=True, slots=True)
class StreamWriterFence:
    run_id: str
    owner_id: str
    incarnation_id: str
    fencing_token: int

class GuardedEventJournal(EventJournal, Protocol):
    async def append_guarded(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult: ...
```

### 5.2 Backend保证

backend在同一个storage transaction/critical section内：

```text
check canonical writer lease/fence
check stream version
append all facts
commit
```

任一失败零写入。不能先`require_current()`、释放锁、再普通append。

SQLite实现把writer lease row与stream rows放在同一transaction。File实现必须让writer metadata CAS与JSONL append共享跨进程锁、flush/fsync协议；无法提供则不支持PendingAct。

现有run writer lease若无法与journal原子校验，迁移其canonical owner；不得复制writer fence mirror。

### 5.3 EventFabric

`runtime/events/fabric.py`新增`append_guarded()`，只调用journal guarded operation并dispatch。

- conflict/fenced不自动retry；
- 可在返回conflict后reconcile dispatcher供下一条显式command使用；
- 普通append行为不变；
- PendingAct只能走guarded入口。

### 5.4 SessionFactCommitter

`contracts/ports/session/facts.py`新增`GuardedSessionFactBatch`和`GuardedSessionFactSink`。`runtime/session/committer.py`投影source facts后调用`EventFabric.append_guarded()`。

## 6. Session projection

### 6.1 Persisted events

修改`runtime/session/events.py`、`runtime/session/codec.py`，为第4.5节facts增加persisted event和strict codec，注册`SESSION_EVENT_CLASSES`。

### 6.2 Projection fields

`SessionProjectionState`新增：

```python
pending_act_by_id
pending_action_arguments_by_invocation
approval_by_request_id
external_effect_by_invocation
run_cursor_by_run_id
claim_by_frontier_id
pending_act_schema_activation
run_execution_lifecycle
```

FileOps状态继续由现有FileOps projection拥有，PendingAct projection只保存reference。

### 6.3 Reducer invariants

- 一个run最多一个active PendingAct；
- argument revision从0连续递增；
- approval request绑定exact argument revision/digest；
- approval terminal唯一，状态仅APPROVED/REJECTED/CANCELLED；
- ExternalEffectStarted要求approval为NOT_REQUIRED或APPROVED；
- ExternalEffect terminal要求已有STARTED；
- IN_DOUBT只用于ExternalEffect；
- receipt是structured result owner；ToolResult message引用同receipt ID/digest并匹配presentation digest；
- rejected/cancelled decision与result、SKIPPED、batch settlement、cursor OBSERVE同批；
- ExternalEffect B与receipt、ToolResult、batch settlement、cursor OBSERVE同批；
- cursor ACT必须引用active PendingAct；
- committed output与active cursor冲突；
- claim renew保持fence，takeover推进fence；heartbeat不推进frontier revision；
- tool/definition/effect变化不能作为argument revision。
- TurnInterrupted提交后拒绝该run的一切正常模型、工具和结果提交；
- 中断后的PendingAct只允许receipt reconciliation与取消结算；
- `<turn_aborted>`由TurnInterrupted投影生成，不得反向解析文本恢复状态，也不得成为独立Message。

Reducer负责完整性验证，不替代guarded append并发控制。

## 7. RuntimePendingActService

新增`runtime/session/pending_act.py`，实现Contracts command/query Ports。

构造依赖：

```text
SessionLiveProjection snapshot query
GuardedSessionFactSink
InferenceCheckpointPort
ToolCompositionDefinitionResolver
FileOps transaction query
PendingAct claim service
```

每个command：

1. 读取immutable projection snapshot与stream version；
2. 校验expected frontier/cursor revision；
3. 构造完整fact batch；
4. guarded append exact version/fence；
5. 返回APPLIED/CONFLICT/FENCED；
6. 不在内部读取新version后自动重试。

### 7.1 A0 command

原子提交：ToolCall messages、frontier、argument revision 0、cursor ACT、checkpoint consumed。

### 7.2 Approval commands

- request：ApprovalRequested + cursor ACT；
- approve：ApprovalDecision(APPROVED)；
- reject/cancel：decision + ToolResult + SKIPPED + PendingActSettled + cursor OBSERVE。

### 7.3 External commands

- start：ExternalEffectStarted + cursor ACT；
- finish：receipt + ToolResult reference + result + SKIPPED + settlement + cursor OBSERVE；
- in_doubt：ExternalEffectInDoubt + blocked PendingAct + cursor ACT/continue false。

### 7.4 LOCAL/PURE commands

- LOCAL B验证FileOps receipt，提交ToolResult/result/settlement/cursor；
- PURE B提交ToolResult/result/settlement/cursor；
- 不创建LOCAL或PURE effect ledger。

### 7.5 Interrupt commands

新增`interrupt_run()`：使用当前stream version与run writer fence提交`TurnInterruptedEvent`，成功后返回typed `RunInterruptPermit`。只有持有该permit的调用方才能触发当前run的进程内cancellation token。

新增`settle_interrupted_pending_act()`：

- 未开始action生成`CANCELLED_BY_USER` ToolResult；
- EXTERNAL STARTED接收优雅退出返回的可信receipt，或只查询provider/receipt，不invoke；无法确认提交`ExternalEffectInDoubtEvent`；
- LOCAL只查询canonical FileOps transaction，不重新执行；
- PURE未提交结果时直接取消，不重算；
- 原子提交单一ToolResult（原工具返回 + 中断说明 + 可选`IN_DOUBT`警告）、`PendingActInterruptedEvent`、`RunRecoveryCursorAdvancedEvent(OBSERVE, false)`、`TurnInterruptedContextAttachedEvent`与`TurnInterruptSettledEvent`；
- command幂等，且只接受已存在的`TurnInterruptedEvent`。

无PendingAct时，第二阶段把fragment附加到取消时最后一条协议完整消息，并提交`TurnInterruptedContextAttachedEvent`与`TurnInterruptSettledEvent`。若第一阶段后进程退出，或缺少`TurnInterruptSettledEvent`，restore返回`InterruptedExecutionNeedsSettlement`。Runtime reconciler补齐第二阶段，但不持有ToolExecutor invoke permit，结构上不能执行新副作用。

## 8. Durable approval coordinator

### 8.1 Policy API

重构`runtime/tools/policy.py::DefaultToolCallPolicy.authorize()`：

```python
AuthorizationEvaluation = Allowed | Denied | ApprovalRequired
```

`PermissionEngine.check()/check_multi()`返回ASK，不再调用`_ask_user`。Hook、extensions、permission和sandbox顺序保持现有策略。

### 8.2 Coordinator

新增`runtime/tools/approval.py`：

```python
existing = pending_act.approval(request_id)
if existing is terminal:
    return existing

await pending_act.request_approval(...)
decision = await interaction.request_approval(request)
return await pending_act.commit_approval(decision)
```

decision durable后响应丢失，按request ID返回同一结果；commit失败仍WAITING。Approval没有IN_DOUBT。

`allow_session` rule与decision同批提交，并从Session projection恢复RuleStore。

### 8.3 参数编辑

用户编辑参数时提交`PendingActionArgumentsRevisedEvent`：

- stable invocation ID；
- revision+1；
- previous/new digest；
- frozen new arguments。

随后重新跑hook/classification/permission。旧approval不适用新revision。Tool name、definition或effect变化时终结旧action并要求新logical invocation。

### 8.4 Product ports

迁移AGUI、ACP、Terminal、Textual：

- 展示Runtime request ID；
- response回传同一ID；
- UI不生成identity；
- disconnect只取消waiter，不取消durable request。

删除Role Tool capability直接注入PermissionEngine的审批路径，只注入窄interaction Port。

## 9. Execution claim

新增`runtime/session/pending_act_claim.py`，claim durable facts仍在Session。

```text
claim_revision：每次claim command递增
fencing_token：acquire/takeover generation递增，renew保持
stream_version：Session CAS
frontier_revision：PendingAct领域变化，heartbeat不影响
```

实现acquire/renew/takeover/release：

- takeover验证旧absolute expiry；
- 不生成wall-clock推导的Expired fact；
- facts为Acquired/Renewed/TakenOver/Released；
- terminal/blocked frontier拒绝新invoke。

`begin_invoke()`返回绑定claim/fence/frontier revision/invocation ID的typed permit。EXTERNAL调用开始后ownership loss，新owner只能查询原invocation；不能自动重做。

## 10. Tool pipeline

### 10.1 固定阶段

修改`runtime/tools/tool_pipeline.py`：

```text
Resolve
→ AuthorizationEvaluate
→ DurableApproval（按需）
→ AuthorizationRecheck
→ ExecuteByEffectKind
→ SessionSettlement
```

`ExecuteByEffectKind`：

- PURE：permit校验后执行；失败恢复可重算；
- LOCAL：进入确定性FileOps transaction，只调用canonical FileOps；
- EXTERNAL：先提交ExternalEffectStarted，再取得invoke permit并调用远端。

这些是ToolExecutor内部固定阶段，不是Graph node或插件点。

### 10.2 删除ToolEffectStore

迁移并删除：

- `runtime/tools/effect_store.py`生产模块/exports；
- ToolExecutor构造中的effect store；
- `ToolEffectStoreConfig`及enabled开关；
- `LedgerStage`；
- ToolSettlement独立JSONL写；
- tool views中的store暴露。

External receipt replay从Session projection读取，不invoke。

## 11. ActionExecutionService 与 Graph

### 11.1 ActionExecutionService

```python
async def execute(turn: ModelTurn):
    frontier = await create_a0(turn)
    return await drive(frontier)

async def resume(frontier: PendingActFrontier):
    return await drive(frontier)
```

`drive`按ordinal串行，根据独立projections判断：

```text
Approval WAITING       → 恢复同一request
Approval REJECTED      → replay terminal result
EXTERNAL STARTED       → query/idempotent recovery/IN_DOUBT
LOCAL                  → query FileOps transaction
PURE未有result          → safe rerun
已有result              → 不执行
TurnInterrupted         → 不进入drive；交给interrupt settlement
```

首个失败/拒绝/取消/IN_DOUBT后同批提交后续SKIPPED。

### 11.2 Unified restore

新增`runtime/session/execution_restore.py`，一次读取同一projection snapshot与stream version，返回：

```text
CommittedExecution
PendingActExecution
InterruptedExecution
InterruptedExecutionNeedsSettlement
InDoubtExecution
ObserveExecution
NoPendingExecution
UnrecoverablePreV1Execution
```

不扫描消息尾部。pre-v1只看`PendingActSchemaActivated`与run lifecycle facts。

### 11.3 RestoreNode

修改`kernel/execution/graph/nodes.py`：

```python
match await restore.snapshot():
    case CommittedExecution(result):
        return End(result)
    case InterruptedExecution(...):
        return End(None)
    case PendingActExecution(frontier):
        state.turn = RecoveredPendingAct(frontier)
        return Transition(ACT)
    case ObserveExecution(continue_inference):
        state.continue_inference = continue_inference
        state.initial_observe_complete = True
        return Transition(OBSERVE)
    case NoPendingExecution():
        return Transition(OBSERVE)
    case InDoubtExecution(...):
        raise ExternalEffectOutcomeInDoubt(...)
```

`RestoreNode.allowed_targets={ACT, OBSERVE}`。Review/refine topology遇PendingAct返回typed topology mismatch。

`InterruptedExecutionNeedsSettlement`在进入Graph前由Runtime host运行receipt-only reconciliation并提交取消结算；它可以读取receipt并写Session facts，但没有invoke权限。Graph绝不把它映射到ACT。结算完成后restore得到`InterruptedExecution`并结束。

### 11.4 ActNode

```text
ModelTurn           → actions.execute
RecoveredPendingAct → actions.resume
```

Act完成后cursor已由B持久化为OBSERVE；节点只返回对应Graph transition，不在进程内创造第二真相。

### 11.5 Product interrupt wiring

ACP `session/cancel`、CLI/Textual interrupt与其他Product入口统一调用Runtime interrupt service：

```text
guarded append TurnInterrupted
→ obtain RunInterruptPermit
→ cancellation_token.cancel()
→ bounded graceful wait
→ force-cancel local task if needed
→ settle interrupted PendingAct
→ commit TurnInterruptSettled
```

不得直接以`asyncio.Task.cancel()`作为公开取消语义。进程内取消失败不撤销durable中断事实。

模型可见context fragment固定为：

```xml
<turn_aborted>
The user interrupted the previous turn on purpose. Any running unified exec processes may still be running in the background. If any tools/commands were aborted, they may have partially executed.
</turn_aborted>
```

存在未配对ToolCall时，对应ToolResult presentation保留恢复出的原工具返回，并附加用户中断说明。正常成功/失败receipt只用于原样恢复ToolResult，不产生额外副作用段；只有`ExternalEffectInDoubtEvent`需要追加副作用恢复警告。不得输出空的副作用部分，也不得为同一ToolCall生成第二个ToolResult。Claude native tools中不得在`tool_use`与对应`tool_result`之间插入任何其他消息或content block。

ToolResult已经齐全时，fragment anchor是最后一条ToolResult消息；只有UserMessage且Think尚未产出时，anchor是该UserMessage。attachment绑定原run ID与anchor message ID，重复取消不得重复附加。不得为中断单独创建user/developer Message。该批flush成功后才能发布取消完成事件。

`TurnInterruptedContextAttachedEvent`保存anchor，不改写原Message。Context projector按“原工具返回、中断说明、可选IN_DOUBT警告”生成最终ToolResult content；普通receipt不得触发警告段。

## 12. Tool definition 可验证重建

扩展snapshot contract，持久化`ToolCompositionDefinitionRef`：

```text
blueprint identity/version
executable package/content digest
composition generation
catalog/protocol fingerprint
tool semantic identities
provider/backend descriptor digest
sandbox/hook/permission generation
capability fingerprint
```

Product generation输入包含全部字段；任一变化推进identity。

`RuntimeToolSnapshotManager.restore(ref)`使用可信blueprint重建候选并逐项比较。全部一致才重新pin等价binding；否则typed fail closed。不得称为恢复原Python binding。

retention覆盖PendingAct settled、cursor推进、claim释放、artifact/result结算、legal hold和Session retention。

## 13. ToolEffectReceipt 与 ToolResult 实现

`ToolSettlement`构造immutable receipt但不独立落盘。RuntimePendingActService在同一B batch提交：

```text
ExternalEffectFinished(receipt)
MessageAppendedEvent(ToolResult with receipt ref/digest)
PendingActionResultCommitted
PendingActionsSkipped（如有）
PendingActSettled
RunRecoveryCursorAdvanced(OBSERVE)
```

B commit失败时Session仍为STARTED；内存receipt不是canonical。恢复查询provider/幂等接口，无法确认则提交IN_DOUBT并typed block。

## 14. 旧数据与迁移

- v1启用提交`PendingActSchemaActivatedEvent`；
- 每个新run提交typed lifecycle/cursor；
- pre-v1 interrupted run只依据activation/lifecycle识别；
- 不检查ToolCall/ToolResult消息尾部；
- 旧Session普通新输入创建v1 run；
- `tool-effects.jsonl`保留至原Session retention，生产不读取；
- 不导入、不双读、不保留compat wrapper。

## 15. 实施阶段

### Phase 1：Contracts与guarded journal

1. PendingAct、Approval、ExternalEffect、Cursor、Claim DTO；
2. strict source facts/decoder；
3. GuardedEventJournal原子version+writer fence；
4. EventFabric/SessionFactCommitter guarded入口；
5. backend并发测试。

### Phase 2：Session projection与Runtime service

1. persisted events/codec；
2. projection独立状态模型；
3. RuntimePendingActService A0/approval/external/local/pure commands；
4. cursor；
5. fault injection。

### Phase 3：Approval与claim

1. PermissionEngine返回ASK；
2. DurableApprovalCoordinator；
3. argument revision；
4. Product ports；
5. claim/permit完整生命周期。
6. durable interrupt service、run cancellation token与Product入口接线。

### Phase 4：Tool pipeline与snapshot

1. effect-kind执行分支；
2. FileOps reference/recovery；
3. ExternalEffect Session facts；
4. definition ref验证重建；
5. 删除ToolEffectStore链。

### Phase 5：Graph与restore

1. unified restore snapshot；
2. ExecutionState recovered variant；
3. RestoreNode ACT/OBSERVE；
4. ActionExecutionService execute/resume；
5. Review/refine topology gate。

### Phase 6：清理与门禁

1. 删除旧approval callback、effect config/store、宽transaction API；
2. pre-v1 typed behavior；
3. architecture artifacts；
4. Pyright、定向测试、相关全量测试；
5. rg确认无混合状态、双store、历史猜测或compat。

## 16. 测试规划

新增：

```text
ztest/contracts/test_pending_act.py
ztest/contracts/test_approval_state.py
ztest/contracts/test_external_effect_state.py
ztest/events/test_guarded_session_append.py
ztest/session/test_pending_act_projection.py
ztest/runtime/test_pending_act_transaction.py
ztest/runtime/test_pending_act_claim.py
ztest/runtime/test_execution_restore.py
ztest/runtime/test_durable_approval.py
ztest/runtime/test_tool_definition_recovery.py
ztest/flow/test_pending_act_graph_recovery.py
ztest/executor/test_pending_act_pipeline.py
ztest/architecture/test_pending_act_boundaries.py
```

关键断言：

- PendingAction没有混合state enum；
- Approval APPROVED不等于External STARTED；
- approval response丢失不重复问；
- argument revision使旧approval失效；
- PURE安全重算；
- LOCAL只通过FileOps恢复；
- EXTERNAL先STARTED后invoke；
- IN_DOUBT阻止自动重试；
- receipt/ToolResult/cursor原子；
- cursor ACT/OBSERVE正确恢复；
- writer fence/version原子CAS；
- renew不推进fence；
- snapshot mismatch fail closed；
- pre-v1不扫描消息。
- durable中断提交后才触发进程内取消；
- 中断后的迟到模型输出被拒绝；
- 中断PendingAct只对账，不自动恢复Act；
- `<turn_aborted>`固定fragment已落盘且不作为恢复依据；
- ToolCall/ToolResult邻接受协议测试保护，中断不得生成独立user/developer Message；
- 中断ToolResult保留原工具返回并附加中断说明；只有IN_DOUBT渲染副作用恢复警告；
- `TurnInterruptSettledEvent`幂等封闭取消结算，marker与ToolResult不重复。

## 17. 完成定义

实现状态（2026-08-06）：Phase 1—6已闭合。Session stream是PendingAct、Approval、ExternalEffect、Result、Cursor、Claim与Interrupt的唯一durable owner；恢复期reconciler只依赖typed result query与guarded Session fact sink，不持有ToolExecutor或dispatch能力。Product composition可以显式注入`ExternalEffectResultQuery`：仅当查询结果的完整invocation identity和presentation digest均严格匹配时，同一guarded batch提交`ExternalEffectFinished + ToolResult + PendingActionResultCommitted`；未知结果原子结算为`IN_DOUBT`，不会盲重试。统一restore通过泛型`CommittedExecution`返回已提交终态，Output Runtime仅以Contracts-owned `CommittedExecutionQuery`暴露不可变查询，Session不依赖具体`OutputEngine`，Kernel也不再执行第二次output restore；已提交终态与active PendingAct并存时fail closed。恢复ACT前由canonical snapshot manager按完整definition identity重建并验证tool snapshot，不触发inference。

验证采用小批串行测试以避免WSL资源峰值；相关PendingAct、restore、interrupt、graph recovery、architecture门禁及定向Pyright均通过。验收期间进一步发现并修复了file-backed writer的嵌套锁问题：`GuardedAppendAuthority`现在在同一个Runtime lease coordinator临界区中原子校验Session stream epoch与run writer epoch，随后由Journal在该临界区内完成stream-version CAS与append，不再分别获取两个同源文件锁。多action恢复也统一按invocation identity跟踪已结算action与新receipt：已提交action不会再次进入结果投影，新external receipt不会因跳过前序action而错绑。仓内既有Pydantic/第三方deprecation warning不属于本切片。

- Approval与Execution contract、projection、service完全分离；
- PendingAct只关联identity与batch；
- Session是唯一durable owner；
- ToolEffectStore生产路径删除；
- stale worker无法append/invoke/settle；
- ToolCall持久后崩溃可从cursor恢复ACT；
- ToolResult持久后崩溃可从cursor恢复OBSERVE；
- 用户中断持久化，重启后不会恢复被中断的Think或Act；
- 已开始副作用按真实receipt或IN_DOUBT结算；
- 审批无IN_DOUBT，External IN_DOUBT不自动重试；
- LOCAL不复制FileOps状态机；
- 无历史尾部猜测、legacy/fallback、双读/双写；
- 架构门禁、Pyright和测试通过。
