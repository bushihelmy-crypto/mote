# PendingAct Frontier 持久恢复需求

## 1. 目标

本文定义 ReAct Graph 在模型已产生 ToolCall、但 Act 尚未完成时的持久恢复语义。

目标场景：

```text
模型产生 ToolCall
→ ToolCall 已持久化
→ 审批、工具执行或结果提交期间进程退出
→ Session resume 后继续同一 Act，不重新调用模型、不盲目重复副作用
```

恢复不得根据历史最后一条消息或缺少 ToolResult 猜测。恢复只读取严格版本化的 Session facts。

## 2. 核心设计决定

1. Approval 与工具执行是两个独立状态机。
2. Approval 是执行的前置门禁，但不拥有执行状态。
3. External effect、LOCAL FileOps 和 PURE 工具使用各自真实 owner，不强行统一成一个 action 状态枚举。
4. `PendingActFrontier` 只关联一批 action、审批、执行与结果事实，不复制这些 owner 的状态机。
5. Session stream 是 PendingAct、Approval、ExternalEffect、ToolResult 和 RunRecoveryCursor 的唯一 durable owner。
6. 现有 `ToolEffectStore/tool-effects.jsonl` 退出生产读写链，不与 Session 双写。
7. `RestoreNode` 一次读取 revision-consistent restore snapshot，根据 durable cursor 进入 `ACT`、`OBSERVE` 或 `END`。
8. `Think` 不判断 PendingAct。
9. `ActNode` 与 `ToolExecutor` 保持唯一；恢复和正常路径共享同一 pipeline。
10. 不新增 Approval Graph node。审批仍是 Act pipeline 的前置任务。
11. 用户中断必须先写入 Session，再通知进程内 cancellation token；重启后不得自动恢复被用户中断的 Think 或 Act。
12. `<turn_aborted>` 是结构化中断事实的模型上下文投影，不是恢复依据。

## 3. 状态模型分离

### 3.1 Approval 状态

```text
NOT_REQUIRED

WAITING
├─→ APPROVED
├─→ REJECTED
└─→ CANCELLED
```

含义：

- `NOT_REQUIRED`：policy 判定无需用户审批；
- `WAITING`：durable approval request 已提交；
- `APPROVED`：用户已批准绑定的工具、参数 revision 与权限目标；
- `REJECTED`：用户拒绝；
- `CANCELLED`：审批请求被合法取消。

Approval 没有 `IN_DOUBT`：

- decision commit 成功，结果确定；
- commit 失败，仍为 `WAITING`；
- 客户端响应丢失时按稳定 request ID查询；
- Session 不可读是 storage/recovery failure，不是审批状态。

### 3.2 ExternalEffect 状态

仅不可安全重放的 EXTERNAL 工具需要：

```text
NOT_STARTED
→ STARTED
├─→ SUCCEEDED
├─→ FAILED
└─→ IN_DOUBT
```

- `STARTED`：外部调用意图已在 Session durable commit，之后才允许调用远端；
- `IN_DOUBT`：外部动作可能发生，但无法查询或取得可信结果；禁止自动重试。

`IN_DOUBT`只属于外部副作用执行，不属于审批。底层 fact 可以说明 `STARTED` 是 write-ahead intent 的 durable commit。

### 3.3 LOCAL FileOps 状态

LOCAL 文件变更不建立第二套状态机。它完全复用 canonical FileOps transaction：

```text
FileOps transaction prepared
→ committed / aborted / in_doubt
```

PendingAct 只保存确定性的 FileOps transaction reference，并从 FileOps receipt投影 action结果。

### 3.4 PURE 工具

PURE 没有副作用状态。崩溃后允许使用相同 action identity重新计算。

### 3.5 Approval 与执行的唯一关系

```python
can_start_execution = approval_state in {
    NOT_REQUIRED,
    APPROVED,
}
```

执行只能读取审批结果，不能推进或重写审批状态。

## 4. Canonical contracts

### 4.1 PendingActFrontier

```python
@dataclass(frozen=True, slots=True)
class PendingActFrontier:
    schema_version: Literal[1]
    frontier_id: PendingActFrontierId
    session_id: SessionId
    run_id: RunId
    model_call_id: ModelCallId
    revision: int
    definition_ref: ToolCompositionDefinitionRef
    actions: tuple[PendingAction, ...]
```

它不包含 `next_node()`、IO、ToolExecutor、Role 或 live catalog。

### 4.2 PendingAction

```python
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
    approval_request_id: ApprovalRequestId | None
    fileops_transaction_id: FileTransactionId | None
```

PendingAction 只保存关联 identity；不复制 ApprovalState、ExternalEffectState 或 FileOpsState。

### 4.3 参数 revision

A0 创建 immutable revision 0：

```python
PendingActionArgumentsRevision(
    invocation_id,
    revision=0,
    arguments,
    arguments_digest,
)
```

用户或 Hook 修改参数时追加新 revision：

```text
stable invocation_id
+ revision + 1
+ previous digest
+ new frozen arguments/new digest
```

规则：

- `ToolInvocationId` 保持不变；
- `ToolInvocationIdentity.with_arguments()`生成绑定新digest的版本；
- Approval 只批准其绑定的arguments revision/digest；
- 旧Approval不能授权新revision；
- Tool name、definition identity、catalog generation不可原地修改；
- 新参数若导致ToolEffect类别变化，旧action以typed superseded结果结束，必须创建新的logical invocation和新审批。

## 5. RunRecoveryCursor

PendingAct完成后 Graph 下一步不放进PendingAct状态。Session拥有独立、窄的run cursor：

```python
@dataclass(frozen=True, slots=True)
class RunRecoveryCursor:
    run_id: RunId
    revision: int
    next_node: Literal[NodeId.ACT, NodeId.OBSERVE]
    pending_act_id: PendingActFrontierId | None
    continue_inference: bool
```

转换：

```text
A0 commit                         → next_node=ACT
Approval WAITING                 → next_node=ACT
工具仍在执行/恢复                 → next_node=ACT
整批 ToolResult 已原子提交        → next_node=OBSERVE, continue_inference=True
明确停止                         → next_node=OBSERVE, continue_inference=False
用户中断                         → TurnInterrupted/END，不自动继续Graph
最终输出 committed               → CommittedExecution/END
```

PendingAct 只表示 Act batch；Graph 下一节点独立由 RunRecoveryCursor 表达。

## 6. 单一事实链与原子边界

所有 PendingAct、Approval、ExternalEffect、ToolResult 和 cursor facts 写入同一个 Session stream。

### 6.1 A0：接收 ModelTurn

一次 guarded append：

```text
AI ToolCall Message(s)
+ PendingActCreated
+ PendingActionArgumentsRevision(revision=0)
+ RunRecoveryCursor(next=ACT)
+ InferenceCheckpointConsumed
```

A0 成功只表示 action durable accepted，不允许执行工具。

### 6.2 Approval request

需要审批时一次 guarded append：

```text
ApprovalRequested(WAITING)
+ RunRecoveryCursor(next=ACT)
```

Approval request ID由以下内容确定性生成：

```text
frontier ID
+ invocation ID
+ arguments revision/digest
+ permission-target digest
```

### 6.3 Approval decision

批准：

```text
ApprovalDecision(APPROVED)
```

批准只解除执行门禁，不表示工具已经开始。

拒绝或取消时一次 guarded append：

```text
ApprovalDecision(REJECTED/CANCELLED)
+ 当前action typed ToolResult
+ 后续未开始action的SKIPPED results
+ PendingActSettled
+ RunRecoveryCursor(next=OBSERVE, continue_inference=True)
```

不得先提交decision再补terminal facts。

### 6.4 EXTERNAL 执行开始

只有Approval为`NOT_REQUIRED/APPROVED`时，才允许一次 guarded append：

```text
ExternalEffectStarted(
    final ToolInvocationIdentity,
    approval reference,
    permission/sandbox/hook decision,
    execution claim fence,
)
+ RunRecoveryCursor(next=ACT)
```

commit 成功后才能调用远端。

### 6.5 EXTERNAL 结果

一次 guarded append：

```text
ExternalEffectFinished(SUCCEEDED/FAILED, receipt)
+ ToolResult Message(receipt reference/digest)
+ action result
+ 必要的后续 SKIPPED results
+ PendingActSettled
+ RunRecoveryCursor(next=OBSERVE, continue_inference=True)
```

如果外部结果无法确认：

```text
ExternalEffectInDoubt(evidence)
+ PendingActBlocked
+ RunRecoveryCursor(next=ACT, continue_inference=False)
```

`IN_DOUBT` 是事实性阻塞，不再转换为其他执行状态。用户若明确要求重试，必须创建新的logical invocation。

### 6.6 LOCAL

流程：

```text
Approval gate satisfied
→ 使用A0已确定的FileOps transaction ID进入canonical FileOps
→ FileOps prepared/commit/abort
→ PendingAct B batch引用FileOps durable receipt
→ ToolResult + PendingActSettled + Cursor(OBSERVE)
```

Session不复制FileOps状态机。崩溃恢复先查询FileOps transaction，不能直接重复文件修改。

### 6.7 PURE

Approval gate satisfied后可执行。完成后一次 guarded append ToolResult、action result、batch settlement与Cursor(OBSERVE)。崩溃前未提交结果时允许按相同identity重算。

### 6.8 用户中断

用户中断是独立的 Turn lifecycle 事实，不属于 Approval、ExternalEffect 或 PendingAct action 状态。

第一阶段必须在触发进程内取消前执行一次 guarded append：

```text
TurnInterrupted(run_id, model_call_id, reason=USER_INTERRUPTED, interrupted_at)
```

该事实提交后，当前 run 不得再提交新的模型输出、ToolCall、Approval request、effect start或正常ToolResult。随后才通过当前run的cancellation token停止模型、审批等待或工具协程。provider不支持取消时可以继续远端计算，但迟到模型输出必须丢弃。

若中断时尚无PendingAct，不生成ToolResult。若已有PendingAct，则第二个guarded append按事实结算：

```text
未开始action                 → ToolResult(CANCELLED_BY_USER)
EXTERNAL STARTED             → 查询receipt；无法确认则ExternalEffectInDoubt + ToolResult(IN_DOUBT)
LOCAL已进入FileOps transaction → 查询FileOps receipt，按真实状态结算

+ PendingActInterrupted
+ RunRecoveryCursor(next=OBSERVE, continue_inference=False)
+ TurnInterruptedContextAttached
+ TurnInterruptSettled
```

无PendingAct时，第二批把fragment附加到取消时最后一条协议完整消息，并提交`TurnInterruptedContextAttached`与`TurnInterruptSettled`。取消结算可以使用ToolExecutor在优雅退出期间返回的可信receipt，或查询既有provider/FileOps receipt，但不能重新执行工具。若进程在两个批次之间崩溃，恢复返回`InterruptedExecutionNeedsSettlement`，由无invoke权限的reconciler补齐结算，不进入Act pipeline。

同时持久化模型可见的context fragment：

```xml
<turn_aborted>
The user interrupted the previous turn on purpose. Any running unified exec processes may still be running in the background. If any tools/commands were aborted, they may have partially executed.
</turn_aborted>
```

该描述与Codex对齐，由`TurnInterrupted`确定性生成并在取消结算时落盘。它不是独立Message，必须遵守工具协议的邻接约束：

- 存在未配对ToolCall时，每个ToolCall仍只生成一个ToolResult；该ToolResult保留工具的正常返回文本，并附加用户中断说明；
- 工具正常返回、receipt可恢复或普通成功/失败都不自动生成副作用段；receipt是恢复证据，不等于模型可见副作用文本；
- 只有存在必须告知模型的副作用恢复警告（当前为`IN_DOUBT`）时，才额外渲染副作用段；
- ToolResult已经齐全时，不生成第二个ToolResult；把中断fragment附加到取消时最后一条ToolResult的presentation末尾；
- 只有UserMessage、Think尚未产出时，把fragment附加到该UserMessage的contextual section；
- 所有分支均提交`TurnInterruptedContextAttached`，不得创建伪user/developer消息；
- 禁止在任一ToolCall与其ToolResult之间插入文本、user message或developer message；
- 一个ToolCall始终只对应一个ToolResult。

中断ToolResult的模型可见presentation固定为：

```text
原工具返回文本（如有）

<turn_aborted>...</turn_aborted>

[仅在存在副作用恢复警告时]
Tool effect: IN_DOUBT，附必要证据。
```

`TurnInterruptedContextAttached`是投影attachment，不修改已经持久化的原ToolResult payload；Context projector在构建模型输入时组合原返回、中断说明和可选恢复警告。不得因为存在receipt而制造额外副作用文本。

`TurnInterruptSettled`证明相关ToolResult已经提交且fragment已经附加到确定的anchor message。anchor message ID与`TurnInterruptedContextAttached`原子提交，保证fragment恰好附加一次。恢复只读取结构化facts，不解析该文本。

## 7. ToolEffectReceipt 与 ToolResult

`ToolEffectReceipt`是EXTERNAL结构化执行结果的authoritative payload，包含：

- invocation identity；
- success/failure disposition；
- provider/process evidence；
- artifact/media/file-change references；
- presentation digest。

`ToolResult Message`是模型上下文投影：

- metadata引用receipt ID与digest；
- content是模型可见presentation；
- 不保存第二份可独立修改的structured result；
- reducer校验presentation digest一致。

receipt、ToolResult、batch settlement和cursor在同一次 Session append提交，不存在canonical receipt与ToolResult之间的合法崩溃窗口。

## 8. Append-time CAS 与 writer fence

EventFabric进程锁不足以防止跨worker stale write。guarded journal backend必须在同一个storage transaction或不可分割critical section中原子检查：

```text
expected stream version
+ run writer owner/incarnation/fencing token
→ append all facts
```

要求：

- 任一条件不匹配，零写入；
- guarded append冲突后不得自动换最新version重试；
- 无法提供fence-conditioned CAS的backend不能启用PendingAct；
- 不允许`require_current()`返回后释放lease lock再执行普通append；
- command重试必须重新读取统一restore snapshot并重新作领域决定。

## 9. Durable execution claim

### 9.1 Claim DTO

```python
PendingActExecutionClaim(
    claim_id,
    frontier_id,
    owner_id,
    incarnation_id,
    claim_revision,
    fencing_token,
    acquired_at,
    expires_at,
)
```

### 9.2 Revision 语义

- `claim_revision`：acquire/renew/release/takeover每次递增；
- `fencing_token`：首次acquire或新owner/incarnation takeover时递增；正常renew不变；
- `stream_version`：每次Session append的CAS版本；
- `frontier.revision`：只随PendingAct领域事实变化；claim heartbeat不推进。

### 9.3 Claim facts

```text
ClaimAcquired
ClaimRenewed
ClaimTakenOver
ClaimReleased
```

expiry本身不按wall clock自动产生fact。只有takeover command验证旧absolute expiry后提交`ClaimTakenOver`，才发生durable状态变化。

### 9.4 Invoke permit

ToolExecutor调用副作用工具前必须取得typed permit，绑定claim、fence、frontier revision和invocation ID。

- stale permit不能invoke或settle；
- provider支持idempotency/fence时必须传递；
- EXTERNAL调用已经开始后claim丢失，新owner只查询结果；不能盲目重复调用；
- provider无法查询且不能幂等恢复时进入`IN_DOUBT`。

## 10. 统一恢复快照

Runtime一次读取同一个verified Session projection snapshot和stream version，返回：

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

映射：

```text
CommittedExecution    → END
InterruptedExecution  → END
InterruptedExecutionNeedsSettlement → 只对账，不进入Graph执行节点
Cursor ACT            → ACT
Cursor OBSERVE        → OBSERVE，并恢复continue_inference
External IN_DOUBT     → typed fail closed
无cursor/无终态        → OBSERVE
```

`RestoreNode`不得分别查询output和PendingAct。committed output与active cursor冲突、多个active frontier或identity fork均fail closed。

## 11. Snapshot 可验证重建

不持久化Python tool instance。`ToolCompositionDefinitionRef`至少绑定：

- Product blueprint identity/version；
- executable package/content digest；
- composition generation；
- catalog/protocol fingerprint；
- tool definition semantic identities；
- provider/backend descriptor digest；
- sandbox/hook/permission generation；
- capability fingerprint。

任一输入变化必须推进generation。只有全部identity/digest匹配approved definition时才能重建等价binding；否则typed fail closed。不得声称恢复原Python对象或退回最新definition。

definition/artifact retention持续到PendingAct settled、cursor已推进、claim释放、无legal hold且满足Session retention。

## 12. 多 Action batch

- 按ordinal串行；
- 首个failed/rejected/cancelled/in_doubt后，后续未开始action同批提交typed `SKIPPED` result；
- skipped保存`blocked_by_invocation_id`与reason；
- skipped没有Approval decision、ExternalEffect receipt或FileOps transaction；
- 恢复不得重新计算skip结论。

## 13. 旧数据治理

- v1启用时提交`PendingActSchemaActivated`，之后每个run写typed lifecycle/cursor facts；
- pre-v1识别只依据activation/run lifecycle metadata，不扫描消息尾部；
- 旧Session无activation fact时不推断pending Act；
- 显式恢复旧interrupted run返回typed `UNRECOVERABLE_PRE_V1_PENDING_ACT`；
- 普通旧Session的新用户输入可以开始新的v1 run；
- 旧`tool-effects.jsonl`按原Session retention保留为archive，生产不读取、不导入；
- 不提供legacy decoder、双读或fallback。

## 14. 验收测试

### 14.1 状态分离

- ApprovalState和ExternalEffectState是不同contract；
- PendingAction不含混合lifecycle enum；
- APPROVED时execution仍可NOT_STARTED；
- execution变化不修改approval facts；
- approval永无IN_DOUBT。

### 14.2 原子性与恢复

- A0、approval request/decision、external start、B各阶段fault injection；
- receipt/ToolResult/cursor同批可见；
- cursor ACT恢复到Act且不调用LLM；
- cursor OBSERVE恢复continue_inference；
- EXTERNAL STARTED后崩溃只查询/幂等恢复或IN_DOUBT；
- PURE安全重算；LOCAL只走FileOps recovery。
- 中断事实先于进程内cancel signal提交；
- Think迟到输出不能在中断后提交；
- Act中断后重启只补齐取消结果，不恢复执行；
- `<turn_aborted>`与结构化中断事实一致，恢复不解析文本。
- Claude/native tool协议下ToolCall与ToolResult严格相邻；中断fragment属于对应ToolResult content，不作为相邻消息插入；
- 一个ToolCall只生成一个ToolResult，不生成独立的中断user/developer消息；
- 缺少TurnInterruptSettled时恢复必定进入取消结算；settled提交后不重复生成marker或ToolResult。

### 14.3 并发

- 两worker同expected version只有一个成功；
- writer takeover与append竞争由backend原子裁决；
- renew只推进claim revision；takeover才推进fence；
- stale claim/permit不能invoke或settle；
- guarded append冲突不自动重试。

### 14.4 Identity与snapshot

- 参数编辑产生immutable revisions；
- old approval不能授权new revision；
- tool/definition/effect变化要求新invocation；
- snapshot任一digest变化fail closed；
- pre-v1识别不读取消息尾部。

### 14.5 架构门禁

- Session是PendingAct/Approval/ExternalEffect/Result/Cursor唯一owner；
- 无ToolEffectStore生产路径；
- Kernel不importRuntime/Product，Runtime不importProduct；
- 无第二ActNode、ToolExecutor、permission evaluator或FileOps状态机；
- 无Any、裸durable dict、字符串状态、局部import、legacy/fallback。

## 15. 非目标

- 不提供IN_DOUBT到其他执行状态的转换；
- 不把Approval提升为Graph节点；
- 不把Approval与Execution揉进同一枚举；
- 不把用户中断揉进Approval或ExternalEffect状态；
- 不持久化Role、tool instance、coroutine、task或live catalog；
- 不从消息尾部恢复；
- 不跨Session与独立store宣称原子；
- 不为旧数据保留生产兼容路径。
