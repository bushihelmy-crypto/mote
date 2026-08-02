# Mote 异步工作统一观测面严格 Tagged Union 实施计划

状态：六次评审修订完成，待最终确认  
范围：`contracts/`、`orchestration/`、`runtime/`、`product/`、`ztest/`  
目标：统一 BackgroundTask 与 WorkflowRun 的产品观测体验，同时保持 identity、owner、lifecycle、durability、recovery、command 和 state truth 完全分离。

## 1. 产品决定

Mote 只统一“用户如何看到异步工作”，不统一“异步工作如何执行”。

```text
AsyncWorkObservation
├── local_background_task
│   └── Agent-owned、process-local、不可恢复、不可跨进程接管
└── durable_workflow_run
    └── Orchestration-owned、durable、revisioned、fenced、可恢复
```

禁止：

- 将 `BackgroundTaskPool` 扩展为 durable executor；
- 让 Workflow definition、checkpoint、continuation 或 resume state 进入 Pool；
- 运行中将 BackgroundTask 自动升级为 Workflow；
- 建立 `AsyncWorkManager`、第三套 registry、第三套状态机或统一 mutable phase；
- 通过裸字符串、ID prefix、alias、fallback 或 duck typing猜测 variant。

## 2. 评审结论与采纳范围

`async-work-observation-tagged-union-implementation-plan-review.md` 的核心结论予以采纳：原计划产品方向正确，但 Port owner、跨 Agent scope、Workflow CAS、字段事实来源、owner-lost、分页、nominal identity、wire/replay 和基础设施复用未闭合，不能直接实施。

唯一调整是测试策略：用户已明确禁止大规模测试以避免 WSL 崩溃。因此本计划以单文件或少量节点分批执行完整架构门禁与受影响子系统测试；不把全量测试设为完成条件，交付时明确未运行范围。

## 3. 当前源码直接反证：先治理，再建设统一观测面

以下不是未来优化，而是已经发现的 R1.5/R2.9 生产闭环反证：

1. `contracts/events/task.py::TaskProgressEvent.task_id` 将 Workflow `run_id` 折叠为 `task_id`。
2. `contracts/task/progress.py::BackgroundTaskProgressEvent.origin` 持有 `WorkflowProgressEvent`，形成错误 domain 嵌套。
3. `product/agents/deferred_projection.py` 对 Workflow 与 BackgroundTask 共用 `task_id` 局部变量和提交文案。
4. `orchestration/workflows/notify.py` 仍使用 `Task {task_id}`、`resume_tasks(task_id=...)`。
5. Workflow query/resume 工具仍通过 `get_bg_pool` 获取混合 facade，Product `AgentBackgroundTasks` 同时拥有 Pool 与 Workflow query/submit/resume/delivery。
6. `DeferredToolSettlement.execution_value: object` 让正式异步 receipt 关系在 adapter 后退化。
7. 旧测试仍存在 Workflow `poll_factory -> pool.submit(..., graph_meta=...)` 的已删除架构语义。

这些反证必须在第一个实施切片归零。受影响的 R1.5、R2.9 及其硬依赖项必须按真实门禁重新签收，不能以当前台账 `DONE` 作为完成证据。

## 4. Slice 0：已经闭合的架构决定

### 4.1 首版产品 scope：当前 Agent，不做伪全局列表

首版统一观测面只服务当前 Agent surface：

- local variant 只查询当前 Agent incarnation 的 Pool；
- durable Workflow variant 只查询 canonical run facts 已绑定当前 caller authority 的 RunId；delivery destination 不参与授权判定；
- `get_async_work(reference)` 与当前 surface 的 observation events 进入首版；
- 不提供跨 Agent tree/application 的 `list_async_work`；
- 不设计双数据源全局分页 cursor。

原因：当前没有不形成第二 Pool registry 的跨 Agent observation routing Port。未来确有 subtree/root 查询需求时，由 Agent supervisor 消费每个 Agent owner 的窄 immutable snapshot Port，并单独版本化 routing、partial-result、timeout、generation watermark 和分页协议；不能扩张首版 service。

### 4.2 Local owner-lost：选择非 durable 语义

BackgroundTask observation 不持久化执行状态，也不建立 durable owner-lost terminal fact：

- 当前进程内，完整 `LocalTaskReference` 可查询 active/latest attempt snapshot；
- process/incarnation authority 仍存活但 reference stale 时，返回 typed `OWNER_LOST` 或 `INCARNATION_LOST`；
- 进程重启后，local task 不恢复到活动列表；
- 调用方持有旧 reference 向新 incarnation 查询时 fail closed，不自动重放或接管；
- 已在进程丢失前提交的 canonical result/artifact fact按其原 owner/retention存在，但不表示 task 可恢复。

因此不新增 durable local observation store、retention owner或第二 BackgroundTask lifecycle。

### 4.3 Workflow nominal identity

在稳定 Contracts owner 建立：

```text
contracts/workflow/identity.py
  WorkflowRunId
  WorkflowDefinitionId
  WorkflowRunReference(run_id, definition_id)
```

两个 ID 与将它们绑定的 canonical reference 必须是验证非空且保留 nominal relation 的 Workflow-owned 正式类型，端到端迁移：

```text
definition/create request
→ WorkflowRunProjection/store/codec
→ AsyncWorkReference
→ query/command/receipt/event/wire
```

删除生产中的 `run_id: str`、`definition_id: str` 旧入口，不保留 alias，也不以 `wfr_` prefix 代替类型。

Workflow governance settlement、Workflow-specific query/control、resume/node inspection、terminal result 和 Orchestration canonical owner 只使用 `WorkflowRunReference`，禁止 import `contracts/async_work`。async-work durable variant 只包装该 reference，不复制 RunId/DefinitionId 字段；Product dispatcher 只在边界解包一次，不用 cast、prefix 或字符串重建。

### 4.3.1 Workflow creation provenance 与 immutable access grant

Workflow create 必须与 Run facts 在同一事务原子提交两个语义不同的 canonical fact：

```text
WorkflowRunCreationProvenance       # immutable audit fact
  workflow_create_admission_id
  creator_logical_agent_id
  creator_incarnation_generation
  creator_lineage_revision
  creator_cancellation_epoch
  creator_session_id
  root_governance_agent_id
  created_at

WorkflowRunAccessGrant              # immutable stable grant
  authorized_logical_agent_id
  root_governance_agent_id
```

- provenance 只记录创建当时的审计身份，不要求恢复后的 incarnation 与 creator incarnation 相等；
- access principal 是稳定 logical Agent，同一 logical Agent 的新 incarnation 只有在 canonical lineage verifier 确认其 active generation、lineage revision 和 owner fence 后才继承访问权，旧 incarnation 立即失权；
- creator Session 只是 provenance；Session close/resume 不终止 durable Workflow，也不单独授权。Session 恢复后仍须通过当前 logical Agent lineage 验证；
- root governance identity 不与 creator 做 OR 字段比较：普通 Agent 命令必须同时满足 authorized logical Agent 与 root containment；root cancellation 只能通过下述受信 durable command delivery 进入 WorkflowRunControl；
- logical Agent terminal 的首版固定语义是：ordinary creator-bound Port 立即因 lineage `NOT_ACTIVE` 失权；durable Workflow 保持当前 canonical run state并继续由已有 Workflow execution/reconciliation owner 推进，不自动取消、不转移 grant、不伪造 settlement；若 run 已 paused，它保持 paused；首版 root supervisor 只通过 scope governance delivery 拥有 cancel authority，不新增 root query/resume 公共面；
- fixed-continue 由 canonical lineage `NOT_ACTIVE` + immutable grant + WorkflowRunProjection 确定性得出；Workflow owner 不订阅 Agent terminal 去写状态，明确不定义 terminal receipt、不枚举 affected runs。需要展示时，observation adapter 只读投影 `creator_unavailable/root_cancel_only`，并从 available actions 删除普通 resume/cancel；
- grant 首版不可 transfer/revoke，也不具有 lifecycle/revision；Agent active/terminal 由 lineage fact证明，root cancellation 由 cancellation epoch/request证明，run cancelling/terminal 由 `WorkflowRunProjection` 证明，禁止写回 mirror state；
- Product retention/purge 只在 run、delivery、effect、artifact pin/hold 全部结算后连同 provenance/grant 一起退出；
- definition identity只证明定义一致性，terminal destination只负责 delivery，二者都不是 access authority；Product 不建 `allowed_run_ids` registry。

query/cancel Port 在 Product composition 时绑定受信 caller context，对外方法不接收可构造的 caller DTO。Runtime 只提供 Contracts-owned 通用 execution caller identity/fence，不生成、签发或理解 Workflow-specific authority。Orchestration adapter 内部消费通用 caller identity、canonical lineage authorization Port 和 `WorkflowRunAccessGrant`完成验证。creation provenance 中的 `creator_lineage_revision` 仅为审计值，恢复查询绝不要求它等于当前 lineage revision。

Workflow create 在进入 run store 前必须取得 Agent governance-owned durable admission：

```text
WorkflowCreateAdmission
  admission_id                 # stable create request identity
  create_request_id
  workflow_run_id              # deterministic mapping from definition + request
  workflow_definition_id
  logical_agent_id
  root_agent_id
  lineage_revision
  cancellation_epoch
  revision
  owner_operation_subject
  owner_fencing_token
  lifecycle: RESERVED | COMMITTED | ABORTED
```

admission 选择方案 A：它只是治理 reservation，不保存 checkpoint/frontier/deadline，不负责重建 run，不成为第二 Workflow create store。`admission_id`、canonical create request identity、definition identity 和确定性 RunId 必须一一绑定，重试不能改变其中任一项。

reserve 与 subtree spawn admission/cancellation snapshot 在同一 lineage owner 同步原语下互斥，并以 expected admission revision + current operation fence 提交。create owner 的唯一顺序是 `RESERVED -> WorkflowRunControl.create -> COMMITTED`；run commit 和 admission settlement 之间崩溃时，reconciler 通过 stable RunId 查到匹配 request/definition/admission provenance 的 run 后幂等结算 `COMMITTED`。

`RESERVED` recovery 先 claim admission，并通过 operation ownership verifier 证明原 create owner fence 已失效；claim 和原 owner 只有一个能以新 revision/fence 提交。若 stable RunId 已存在且 identity/provenance 精确匹配，结算 `COMMITTED`；若确认 run 不存在、原 owner 已失权且 admission 不涉及已执行外部动作，才幂等 `ABORTED`。不允许从 admission 盲目重建 run。

`ABORTED` 是该 admission identity 的 terminal fact；同一 create request 重试返回 typed `PREVIOUS_ADMISSION_ABORTED`，不重开原 admission。调用方若仍要创建，必须提交新 create request identity 并重新通过当前 cancellation/admission 治理；禁止 alias 或 silent retry。cancellation snapshot 等待 frozen `RESERVED` 被 fenced reconciliation 结算；poison/dead-letter 保持 scope 非 `SETTLED` 并进入 supervisor audit，不强制 abort。

cancellation snapshot 原子关闭目标 Agent 的新 Workflow admission，并冻结当时 target AgentId 及已 `RESERVED/COMMITTED` admission identity；因此 snapshot 之后不会漏入新 run，早于 snapshot 但尚未完成 run commit 的 admission 也不会丢失。strict codec 对 identity mapping、revision/fence、lifecycle 和额外/缺失字段 fail closed。

身份与并发数字不复用含义：`AgentId`/`SessionId` 使用或迁移到仓内 authoritative nominal contract；`incarnation_generation` 只表示驻留世代，`lineage_revision` 只表示 lineage fact 版本，`cancellation_epoch` 只表示稳定 subtree 取消世代，`run revision` 只表示 Workflow state CAS，`fencing_token` 只表示当前 operation/lineage owner 的提交权。实施前先迁移现有 lineage/residency 中的同义裸字符串，不复制新 identity owner。`root_governance_agent_id` 必须是 canonical stable root `AgentId`，不允许 root path、tenant、Session owner 或 placement owner 代替。

### 4.3.2 Root cancellation durable command delivery

root/subtree cancellation 仍由 Agent governance 的 canonical cancellation epoch/snapshot 拥有。它不直接改 Workflow store，而是通过消费方窄 Port 提交 durable command intent：

```text
WorkflowGovernanceCancelRequest
  request_id: WorkflowGovernanceScopeCancelRequestId
  root_agent_id
  subtree_agent_id
  lineage_snapshot_revision
  cancellation_epoch
  target_agent_ids: tuple[AgentId, ...]
  admitted_workflow_create_ids: tuple[WorkflowCreateAdmissionId, ...]
  reason: ROOT_CANCELLATION

WorkflowGovernanceCancelAcceptance
  request_id
  disposition: ACCEPTED | IDEMPOTENT | SCOPE_MISMATCH | STALE_EPOCH |
               BACKPRESSURED | FENCE_LOST
  accepted_revision
  target_agent_count

WorkflowGovernanceCancelSettlementSnapshot
  request_id
  revision
  lifecycle: PENDING | RECONCILING | SETTLED | PARTIAL | DEAD_LETTER
  per_run_settlements: tuple[WorkflowGovernanceRunSettlement, ...]

WorkflowGovernanceRunSettlement
  reference: WorkflowRunReference
  per_run_request_id
  revision
  disposition: CANCEL_INTENT_APPLIED | ALREADY_CANCELLING | ALREADY_TERMINAL |
               RETRY_PENDING | DEAD_LETTER
```

- request 必须携带 lineage cancellation owner 在同一个 fenced snapshot transition 中冻结的 target AgentId 与已接纳 Workflow create admission identity。首版集合受 Agent governance cap 严格有界，直接使用 typed tuple；未证明需要时不引入 artifact snapshot。unknown/duplicate/wrong-root target、snapshot revision mismatch 和 stale epoch strict reject；
- scope `request_id` 由 canonical root/subtree identity + cancellation epoch 确定性派生。Workflow owner 仅将 request 中的 target AgentId/admission identity 与 immutable grant/provenance 做确定性 join；不重查当前 subtree、不读 lineage 私有 store；
- Agent cancellation coordinator 是 consumer，Workflow-owned durable command inbox/reconciler 是 implementation owner；首选在现有 durable delivery/reconciliation 的 canonical owner 扩展 typed command variant，不复制 queue/store/retry engine；
- durable accept 前通过 canonical lineage cancellation snapshot verifier Port 校验 snapshot revision、root containment、target/admission 集合和 cancellation epoch；Workflow inbox 不读 lineage 私有 store；
- `submit` 只返回 durable acceptance，不同步返回 per-run settlement。`BACKPRESSURED` 精确表示未 accepted、未写 intent；已 accepted 但尚未处理只表现为 settlement `PENDING`。commit 失败不返回 `ACCEPTED`；
- accept transaction 原子保存 scope request + frozen target/admission snapshot，不要求同事务枚举全部 run。reconciler 每次 scan 做同一确定性 join，以 `hash(scope_request_id, WorkflowRunId)` 幂等提交 per-run intent；accept 后在部分 intent 创建前 crash 不会漏项或双创建；
- `RESERVED` admission 必须先结算为 committed run 或 aborted；scope 只有在 frozen admission 全部结算、所有 joined run 的 cancel delivery 都形成 terminal/idempotent per-run settlement 后才能 `SETTLED`。`CANCEL_INTENT_APPLIED` 只表示 cancel command 已幂等推进到 `CANCELLING`，不表示 run 已 terminal。`PARTIAL` 是仍可由 policy 有界重试的非 terminal settlement；`DEAD_LETTER` 是 retry exhausted 后的 terminal delivery settlement，两者都不伪装 scope 成功，retention 保留 request、per-run receipt 与审计直至 Product policy 允许 purge；
- reconciler 通过 durable scan 重新发现 pending intent，claim 绑定 delivery revision 与 operation ownership fence；进程内 signal 不是唯一推进机制；
- reconciler 读取 canonical run revision并只调用同一 `WorkflowRunControl.cancel()` chokepoint。revision conflict 时重读并有界重试；已 `CANCELLING` 或 terminal 以 typed idempotent settlement 结算；stale claim/fence 不得 commit；
- settlement query 由 Agent cancellation coordinator/supervisor audit 消费，strict codec、retention、poison/backoff 和 purge 跟随 command delivery canonical owner；best-effort notification 只用于唤醒，durable scan/query 保证卡住的 accepted intent 可观测。不使用 access grant 充当 cancel ledger，不建第二 Workflow control path。

### 4.4 Workflow mutation 始终保留 CAS

稳定 reference 不携带 revision；mutation command 单独携带 precondition：

```python
CancelDurableWorkflowRun(
    reference=DurableWorkflowRunReference(...),
    expected_revision=revision,
    reason=WorkflowCancelReason(...),
)
```

cancel、resume 及其他 mutation 均保留 `expected_revision` 或更强的 fenced CAS。Product 不得先查询最新 revision 后替调用方无条件执行。revision conflict、definition mismatch、terminal idempotency 与 ownership loss分别返回 typed receipt。

Local cancel 必须携带完整 `LocalTaskReference`，包含 process、Agent、incarnation、TaskId、AttemptId；不接受跨 Pool 裸 TaskId。

### 4.5 Observation 字段收窄到已有 canonical facts

首版不承诺当前 owner 尚未拥有的时间线、progress history 或聚合 delivery 状态。核心 snapshot 字段如下：

| 字段 | canonical source | durable | 一致性 |
|---|---|---:|---|
| local reference/presentation phase/label/result pointer | Agent-owned Pool immutable snapshot经domain总投影 | 否 | 同一 Pool lock/generation 下读取 |
| local available actions | Background domain 对同一 snapshot 的总投影 | 否 | 不持久化 UI 状态 |
| Workflow reference/revision/presentation phase/frontier/pause detail/deadline/terminal result | `WorkflowRunProjection`经domain总投影 | 是 | 单一 run revision |
| Workflow available actions/creator availability | Workflow run revision + immutable grant + lineage authorization receipt/revision 的总投影 | 可重建 | 显式非原子组合且携带各自revision；不持久化 UI 状态 |
| terminal delivery | reconciliation owner 的 per-destination projection | 是 | 保留 destination identity/revision，不折叠 bool |
| live progress | domain event stream | local best-effort；Workflow携带已观测run revision | 无连续sequence承诺；revision只标识发射方观测的canonical snapshot，不冒充历史真相 |

首版 observation 不包含无法从上述 snapshot证明的 accepted/started/updated/terminal instant 列表，也不把 best-effort progress entries 放入 canonical snapshot。

### 4.6 Result/artifact contract

- Local BackgroundTask 继续复用 `contracts/task/models.py::TaskResultPointer`，但 terminal fact必须额外绑定完整 `LocalTaskReference`，不能仅靠 pointer 内的 TaskId。
- `CompletedStoredTaskResultPointer.output` 一次性迁移为 `ArtifactRef`；`StoredTaskOutput(locator="task-output:...")` 从 canonical terminal result删除，不保留renderer fallback或永久双轨。
- `TaskOutputStore` 若仍服务process-local streaming/progress log，其 reference 绑定完整 local owner/attempt 且不进入 terminal result、durable replay 或 Artifact GC 语义；默认保持 Pool 包内私有，只有真实跨包 reader consumer 才允许提取 `LocalTaskOutputLogReference` 窄 contract。
- 实施前盘点旧 `task-output:` locator：若没有用户durable数据则直接退出；若存在则报告并做一次性auditable migration到Artifact publication，禁止双读。
- Workflow 新增 domain-owned typed terminal result union，绑定 `WorkflowRunId + terminal revision`，内容只允许 success inline bounded result、`ArtifactRef`、typed failure、cancelled或timed-out。
- pause严格属于非terminal run observation的pause detail/checkpoint/frontier；不进入terminal union。
- `IN_DOUBT` 保留在effect或delivery reconciliation projection。除非先正式扩展WorkflowRunPhase及其transition/retention语义，否则不得上提为run terminal result或通用presentation phase。
- 现有 `terminal_payload: str` 必须迁移为严格 versioned terminal result codec；不能让 observation adapter猜测字符串内容。

### 4.7 Multi-destination terminal delivery

Workflow observation 不提供无来源的 `delivery_state`。如果 surface 需要展示交付情况，使用：

```text
WorkflowTerminalDeliveryObservation
  delivery_id
  run_id
  destination_id
  revision
  state
  attempts
  next_eligible_at
  reason
```

一个 Run 可以返回零到多个 immutable per-destination projection。跨 run store/reconciliation store 的组合明确是非原子 read model：每个成员携带自己的 revision；调用方不得把它解释为同一事务快照。canonical run phase 不受 delivery projection反向影响。

### 4.8 Wire 与 replay

边界决定如下：

- Python 进程内 event bus传递 frozen DTO；
- ACP、AG-UI 等 wire surface 使用 `mote.async-work-observation/v1` strict envelope；
- durable Workflow observation从 canonical Workflow run/result/delivery facts重建，不持久化 Product reducer state；
- local observation/progress 不进入 durable replay，进程重启后不恢复；
- local 已提交 terminal result pointer按现有 resource/artifact事实处理，不恢复执行；
- 实施前搜索 session/event/wire 中所有 Workflow-as-task payload，确认不存在需保留的用户 durable 数据；若存在，先报告并选择一次性 migration，禁止生产双读。

### 4.9 Composition 与 lifecycle

- Product composition root 唯一装配 current-Agent local observation adapter 与 Workflow observation adapter。
- aggregator 是无状态 dispatcher，只持有 local/Workflow 两个 observation Port；cancel surface 分别使用两个 command Port；不持有 Pool、task、store、reconciler或 mutable registry。
- local adapter lifecycle跟随当前 Agent incarnation；shutdown 后所有 reference fail closed。
- Workflow adapter通过消费方 Port访问 durable control/query，不暴露 `ProductWorkflowDurability` concrete type。
- surface shutdown只关闭自己的 subscription，不关闭或接管 domain owner。

## 5. Contract 与 Port owner

DTO 与跨层 Port 分离：

```text
contracts/workflow/
├── identity.py
├── authority.py
└── result.py

contracts/async_work/
├── identity.py
├── observation.py
├── command.py       # DTO、receipt、disposition；不放 Protocol
├── codec.py
└── __init__.py

contracts/ports/async_work/
├── observation.py   # observation query 公共定义；不定义 subscription Port
├── local.py         # local query/cancel Port
├── workflow.py      # Workflow query/cancel Port
└── __init__.py

contracts/ports/workflow/
├── governance.py    # root cancellation acceptance + settlement query Ports
└── __init__.py
```

不建立巨型 `AsyncWorkService`。真实消费者与方法：

| Protocol | 最小方法 | consumer | implementation owner |
|---|---|---|---|
| `LocalAsyncWorkObservationPort` | `get(LocalTaskReference) -> LocalObservationQueryResult` | Product current-Agent dispatcher | Agent-owned Background service adapter |
| `LocalAsyncWorkCommandPort` | `cancel(CancelLocalBackgroundTask) -> LocalCancelReceipt` | Product cancel surface | Agent-owned Background lifecycle owner |
| `WorkflowAsyncWorkObservationPort` | `get(WorkflowRunReference) -> WorkflowObservationQueryResult` | Product current-Agent dispatcher解包async-work variant；Port instance已绑定受信caller context | Orchestration Workflow query adapter |
| `WorkflowAsyncWorkCommandPort` | `cancel(CancelWorkflowRun) -> WorkflowCancelReceipt` | Product cancel surface解包async-work command；Port instance已绑定受信caller context | Orchestration Workflow control adapter |

Product 内部 `CurrentAgentAsyncWorkObservationService.get(AsyncWorkReference)` 是无状态同层dispatcher，不新增 Contracts Port。terminal/Textual/ACP/AG-UI只消费它产出的 frozen DTO或wire envelope。live progress继续走现有 Runtime event bus，不新增subscription Protocol或第二 event bus。因此首版准确新增四个跨层 Protocol，不是五个。

上述“四个”仅指 async-work product observation/command 服务面。root cancellation 另复用/extension Workflow governance bounded context 的 command/query 两个窄 Port：

| Protocol | 最小方法 | consumer | implementation owner |
|---|---|---|---|
| `WorkflowGovernanceCancellationDeliveryPort` | `submit(WorkflowGovernanceCancelRequest) -> WorkflowGovernanceCancelAcceptance` | `SubtreeCancellationCoordinator` | Workflow durable command inbox；只承诺 durable acceptance |
| `WorkflowGovernanceCancellationSettlementPort` | `get(request_id) -> WorkflowGovernanceCancelSettlementSnapshot` | Agent supervisor reconciliation/audit | Workflow durable command reconciler projection；最终调用同一 `WorkflowRunControl` |

两个 governance Port 在 Product composition root 分别注入 Agent cancellation coordinator 和 supervisor reconciliation/audit，lifecycle 跟随 supervisor + durable reconciler activation/shutdown；shutdown 不删除 accepted intent，重启后由 durable scan 继续。它们不暴露 Workflow store/control concrete type，不提供普通 Agent 调用入口。

Workflow resume/node inspection 使用独立 Workflow-specific Port，不膨胀通用 async-work Port。

## 6. Strict Tagged Union

### 6.1 Identity

```python
class AsyncWorkKind(str, Enum):
    LOCAL_BACKGROUND_TASK = "local_background_task"
    DURABLE_WORKFLOW_RUN = "durable_workflow_run"

@dataclass(frozen=True, slots=True)
class LocalBackgroundTaskReference:
    kind: Literal[AsyncWorkKind.LOCAL_BACKGROUND_TASK]
    reference: LocalTaskReference

@dataclass(frozen=True, slots=True)
class DurableWorkflowRunReference:
    kind: Literal[AsyncWorkKind.DURABLE_WORKFLOW_RUN]
    reference: WorkflowRunReference

AsyncWorkReference: TypeAlias = (
    LocalBackgroundTaskReference | DurableWorkflowRunReference
)
```

不提供 `.id`/`.task_id` 公共基类。`display_id` 只能由 Product renderer计算，不得用于 command。

### 6.2 Observation

```text
AsyncWorkObservation
├── LocalBackgroundTaskObservation
│   reference, label, AsyncWorkPresentationPhase,
│   LocalBackgroundObservationDetail,
│   result_pointer, available_actions
└── DurableWorkflowRunObservation
    reference, revision, AsyncWorkPresentationPhase,
    DurableWorkflowObservationDetail,
    frontier, pause_detail, deadline, terminal_result,
    available_actions, deliveries[]
```

Contracts DTO不得 import `BackgroundTaskStatus` 或 `WorkflowRunPhase`。两个 detail均为observation-owned frozen tagged DTO，只表达已确认展示事实，不复制domain transition enum：local detail可表达pool lifecycle/pin/owner-loss disposition；Workflow detail可表达pause reason projection和frontier。variant-specific字段不提升为大量 `Optional`。`available_actions` 分别由两个 Orchestration bounded context对同一 snapshot总投影；UI 不得反向写入。

### 6.3 Presentation phase

Contracts 只定义 `AsyncWorkPresentationPhase` enum及observation detail DTO。投影实现分别位于：

- `orchestration/background_tasks/observation.py`；
- `orchestration/workflows/observation.py`。

投影是穷尽总函数，只用于呈现，禁止反向 decode 为 domain state。

### 6.4 Progress

```text
AsyncWorkProgress
├── LocalBackgroundTaskProgress
│   LocalTaskReference, stage, phase, detail
└── DurableWorkflowRunProgress
    DurableWorkflowRunReference, observed_run_revision,
    node_id, phase, detail
```

progress首版是live、best-effort signal，不是canonical history：

- local scope由完整 process/incarnation/TaskId/AttemptId界定，重启后失效；不发明 `local_sequence`；
- Workflow携带发射方已验证的 `observed_run_revision`，不承诺事件与commit构成原子事务或连续序列；
- Workflow durable owner在发射前验证execution fence；stale owner不得发射；
- fence 验证与 best-effort emit 不是原子提交，验证后立即失权的竞态信号可能到达；consumer 必须把 progress 当作可丢弃提示并以后续 canonical snapshot/revision 覆盖，不得由它推进 domain state；
- checkpoint commit成功而emit前crash允许缺失live event；emit后下一commit失败也不改变canonical run state；
- replay从run/checkpoint/terminal/delivery facts重建，不重放best-effort progress；
- 若未来需要无缺口durable progress，必须另建Workflow-owned versioned journal并单独评审，不能由adapter编号。

删除 `BackgroundTaskProgressEvent.origin: WorkflowProgressEvent`。若 BackgroundTask 内部运行本地 pipeline，`ActivityTopology` 仅作为嵌套展示数据，不产生 Workflow identity。

### 6.5 Submission 与 terminal receipt

```text
AsyncWorkSubmissionReceipt
├── LocalBackgroundTaskSubmission(LocalTaskReference)
└── DurableWorkflowRunSubmission(DurableWorkflowRunReference, revision)

AsyncWorkTerminalFact
├── LocalBackgroundTaskTerminalFact(LocalTaskReference, TaskResultPointer)
└── DurableWorkflowRunTerminalFact(
      DurableWorkflowRunReference,
      terminal_revision,
      WorkflowTerminalResult,
    )
```

`DeferredToolSettlement` 的外部动态输入仍可为 `object`，但分类后必须立即解码成 typed submission receipt；正式 observation identity 不得以 `object` 穿过 adapter。

## 7. Command disposition

通用 Product cancel入口接受 strict command union并立即分派：

```text
CancelAsyncWork
├── CancelLocalBackgroundTask(reference, reason)
└── CancelDurableWorkflowRun(reference, expected_revision, reason)
```

receipt 不返回 bool，至少区分：

- local：`CANCEL_REQUESTED`、`ALREADY_TERMINAL`、`OWNER_LOST`、`INCARNATION_LOST`、`STALE_ATTEMPT`、`NOT_FOUND`；
- Workflow：`CANCEL_REQUESTED`、`ALREADY_CANCELLING`、`ALREADY_TERMINAL`、`REVISION_CONFLICT`、`PRINCIPAL_MISMATCH`、`CALLER_NOT_ACTIVE`、`INCARNATION_MISMATCH`、`LINEAGE_REVISION_STALE`、`DEFINITION_MISMATCH`、`CONTROL_UNAVAILABLE`、`CLAIM_CONFLICT`、`FENCE_LOST`、`NOT_FOUND`。

Workflow cancel receipt只承诺canonical transition进入 `CANCELLING`；最终 `CANCELLED` 只能来自terminal settlement。worker/process loss不删除durable run，因此不得复用local `OWNER_LOST`。Workflow resume仍使用 expected revision + resume nonce；BackgroundTask resubmit只允许同 Agent/Pool/process且绑定active/latest AttemptId。

`WorkflowCancelReason` 由 `contracts/workflow/command.py` 定义为封闭enum（首版 `USER_REQUEST`、`AGENT_REQUEST`、`DEADLINE_POLICY`），wire严格编码。`USER_REQUEST` 只来自已验证用户命令来源，`AGENT_REQUEST` 只来自已绑定 Agent Port，`DEADLINE_POLICY` 只由 Workflow deadline owner 选择。root cancellation 使用 governance command 中的固定 typed `ROOT_CANCELLATION`，不伪装普通 caller reason。首版删除无真实 consumer 且与 durable 恢复冲突的 `SESSION_SHUTDOWN_POLICY`。模型自由文本只进入可审计 detail，不影响 authorization、priority 或 discriminator。

## 8. 实施切片

每个切片都是 identity→owner→codec/store→Port→composition→真实 consumer→删除旧面 的纵向闭环，不按目录分批制造临时类型、concrete dependency 或二次迁移。

### Slice 1：Workflow foundation vertical slice

1. 由 canonical ledger 机械计算并退回 R1.5/R2.9 硬依赖闭包；不维护第二份依赖图。
2. 先盘点 authoritative Agent/Session/lineage/operation ownership identity；在原 owner 内建立或迁移 nominal `AgentId`、`SessionId`、incarnation generation、lineage revision 与 fencing token，同片迁移直接消费者，不留同义字符串入口。
3. 建立 `WorkflowRunId`、`WorkflowDefinitionId`、canonical `WorkflowRunReference`、creation provenance、immutable access grant 与 strict `WorkflowTerminalResult`，一次迁移 run model/store/control/codec、governance/query/resume consumer；async-work wrapper 与 dispatcher 同片接入，不保留复制 identity shape。
4. 扩展现有 lineage authorization/admission 为最小 Contracts-owned Port；Workflow create reserve/settlement 与 subtree cancellation snapshot 在 lineage owner 同一同步原语下协调，Runtime 只交付通用 caller execution identity/fence。
5. 建立已绑定 caller context 的最小 Workflow query/control Port，Product 同片装配并迁移至少一个真实 tool/surface consumer；删除该链的 `run_id: str`、裸 terminal payload 和 concrete control 入口。
6. 复用/extension canonical durable delivery/reconciliation 实现 governance acceptance + settlement query Ports，同片注入 Agent cancellation coordinator/supervisor，所有结算只调用同一 WorkflowRunControl。
7. 关闭新 incarnation、stale incarnation/fence、Agent terminal fixed-continue无订阅、Session close/resume、create/cancellation admission 竞争、partial-intent crash/retry、terminal retention 测试和门禁。

### Slice 2：Background/Workflow split vertical slice

1. 在正式 Workflow Port 已存在的前提下拆分 `AgentBackgroundTasks`：Pool service只拥有 local task，Workflow submit/query/resume/delivery进入独立 Product service。
2. Workflow tool capability从 `get_bg_pool` 一次迁移到已绑定 Workflow Port；notify/tool/message 全部使用 nominal RunId，删除 Workflow `task_id` 参数与文案。
3. Background progress 与 Workflow progress拆成平级 variant，删除 `origin` 嵌套、混合 facade、Workflow→Pool 测试语义和所有旧 composition binding。
4. 在 async-work observation 消费 local terminal result 之前，将 canonical stored output 一次性迁移到 `ArtifactRef`并删除 `StoredTaskOutput` terminal variant。若 streaming log 只有 Pool 包内 writer/reader，reference 保持私有；只在存在真实跨包 consumer 时才提取最小 contract。
5. 同片完成 Background/Workflow owner、lifecycle、progress、result/artifact 和 Product service 门禁，不保留 adapter/re-export。

### Slice 3：Async-work observation vertical slice

1. 建立 `contracts/async_work/` strict DTO/codec 与四个窄 Port，同片实现 Background/Workflow domain 总投影。
2. Workflow lifecycle/result 从同一 run revision 读取，access/actions 显式组合 immutable grant 与携带revision的lineage authorization receipt，delivery 保持 per-destination revision；Background 从同一 Pool generation snapshot 读取。
3. Product 同片装配无状态 dispatcher、typed cancel/submission receipt 并接入至少一个 production surface，不产生无消费者公共类型。
4. 同片完成 strict codec、principal/lineage failure、CAS、stale fence、无 observation cache/第二 registry 门禁。

### Slice 4：Remaining surfaces and zeroing slice

1. 迁移 terminal、Textual、ACP、AG-UI、wire/replay 和其余 Product consumer；live progress 继续复用 event bus 且不进入 replay。
2. 删除旧 wire/event/result locator、fallback、renderer guessing 与第二 event/control path，完成全部定向测试。
3. 本切片结束前必须删除并门禁：

- Workflow `task_id` 参数、通知、XML标签、文案；
- local cancel/query 的跨 Pool裸 TaskId入口；
- `BackgroundTaskProgressEvent.origin`；
- `run_id: str`/`definition_id: str` 与 nominal identity并存；
- Product concrete durability充当跨包 contract；
- presentation/replay observation cache；
- 新旧 progress、terminal、submission receipt双事件链；
- ID prefix guessing、alias、re-export、双 decoder、fallback；
- `Any/object` 携带已分类的正式 observation identity。

### Slice 5：证据复核与台账重签

本切片不接受延迟的兼容清理或生产迁移；Slice 4 结束时旧入口、双 wire/event/control path 已必须为零。本切片只重跑小批量证据、核对 canonical ledger closure 并重签台账。

最后根据生产代码、定向测试、类型检查与canonical依赖拓扑重新签收受影响债务项。当前台账快照机械闭包预期为：`R1.5`、`R2.9`、`R1.26`、`R2.1`、`R2.47`、`R2.48`、`R1.20`、`R2.51`、`R2.28`、`R1.13`、`R2.53`；实施时以ledger gate实时计算结果为准，文档列表不是第二真相源，也不得扩张为全部96项退回。

## 9. 基础设施复用与最小服务面证据

| 能力 | 复用决定 | 不新增/不复用原因 |
|---|---|---|
| local identity | 复用 `LocalTaskReference` | 已拥有完整 owner/incarnation/attempt |
| local result | 复用 `TaskResultPointer`，terminal fact补完整 reference并移除stored locator variant | pointer自身TaskId不足以跨Pool授权 |
| large terminal result | 统一为 `ArtifactRef`/publication | `task-output:` 不再作为canonical terminal locator |
| local streaming log | 收窄现有 `TaskOutputStore` 为attempt-bound log reference | process-local日志与durable artifact不变量不同，不强行抽象 |
| Workflow run state | 扩展 canonical `WorkflowRunProjection` owner | Product concrete durability不是 contract |
| Workflow access | immutable provenance/grant与run create同事务存储，实时复用lineage authorization | grant不镜像lineage/run/cancellation lifecycle，destination和Product registry都不能证明授权 |
| root cancellation delivery | 扩展canonical durable delivery/reconciliation的typed scope request、acceptance、settlement query | Agent cancellation snapshot是frozen target/cutoff source，同一WorkflowRunControl是唯一state transition chokepoint |
| Workflow delivery | 复用 reconciliation per-destination record | 不折叠成 run级bool |
| activity topology | 复用 `contracts/activity.py` | 仅展示，不承担 work identity |
| event transport | 迁移现有 Runtime event bus | 不建第二 event bus或 observation store |
| local lifecycle Port | 收窄扩展现有 task lifecycle能力 | 不暴露 Pool concrete/private map |
| Workflow command | 复用 canonical control CAS | 不建立 Product第二control path |

Aggregator隐藏 Pool、store、reconciler、lease、task、lock和routing实现；公开方法只有当前消费者真实需要的 `get`，cancel 由独立 typed command dispatcher 调用两个 command Port。live subscription 直接复用现有 event bus，不在 aggregator 上建第二入口，也不预留全局 manager 能力。

## 10. Wire codec

```json
{
  "schema": "mote.async-work-observation/v1",
  "kind": "durable_workflow_run",
  "payload": {}
}
```

decoder必须：

- `type(raw) is dict`且字段集合精确相等；
- unknown schema/kind fail closed；
- primitive 类型精确，不做 `str/int/bool`强转；
- local variant禁止 Workflow字段，Workflow variant禁止 process/attempt字段；
- nominal identity round-trip不退化；
- malformed/cross-variant payload无法进入 canonical DTO。

实施前完成旧 durable payload搜索。盘点不仅覆盖 RunId/terminal result，还必须分类旧 run 是否缺少 creation provenance、immutable grant 和 create admission mapping。存在历史数据时先报告并选择一次性、可审计、幂等 migration，或经用户授权丢弃；不得伪造不可证明的 creator/access fact。可由旧 canonical session/lineage/create request 证明的 run 生成 migration provenance/grant 与已 `COMMITTED` admission；无法证明的 run fail closed并等待用户明确处置。禁止生产双读或临时缺省 authority。

## 11. 架构门禁

新增/更新小型门禁，至少证明：

1. `contracts/async_work` 不定义跨层 Protocol；Port只位于 `contracts/ports/async_work`。
2. Contracts 不 import上层，phase projection实现不位于 Contracts。
3. union恰有两个显式 variant。
4. local identity完整包含 process/Agent/incarnation/TaskId/AttemptId。
5. Workflow reference使用 nominal RunId/DefinitionId且不含TaskId/AttemptId。
6. `contracts/workflow` 不 import `contracts/async_work`；Orchestration Workflow owner/governance/query/control 只使用 canonical `WorkflowRunReference`，async-work durable variant 只包装它且不复制 ID 字段，Product只解包一次。
7. Contracts observation不 import Orchestration status enum，只携带presentation phase/detail DTO。
8. local query/cancel只接受完整 reference；Workflow mutation保留 expected revision。
9. Workflow create原子提交 immutable provenance 与 immutable access grant；query/cancel Port 在 composition 时绑定受信 caller context，内部校验 logical principal、active incarnation、lineage revision 和 fence；destination不充当授权，grant 不镜像 lineage/cancellation/run lifecycle。
10. Workflow create admission 与 subtree cancellation snapshot/spawn admission 原子协调；admission绑定stable request/RunId/definition，reserve/commit/abort使用revision+fence且reconciler只在旧owner失权后claim；cancel request 持久 frozen target AgentId + admitted create identity，不在恢复时重算历史 cutoff。
11. root cancellation acceptance 与 settlement 分离；`BACKPRESSURED` 必然未写 intent，accepted intent 通过 durable scan/query 可观测，per-run stable identity、claim/fence、revision retry、terminal idempotency 闭合，且只进入同一 WorkflowRunControl。frozen tuple decoder对 governance cap 与重复/wrong-root/unknown identity fail closed。
12. 旧 durable run 迁移必须对 provenance/grant/admission 逐项有可审计证据；不可证明的 authority fail closed，不存在缺省 grant、双读或 fallback decoder。
13. BackgroundTaskPool不持有 Workflow definition/checkpoint/continuation/resume state。
14. Workflow owner不 import BackgroundTaskPool，Workflow production文案/参数不出现task_id。
15. Product dispatcher不持有 Pool/task/store/reconciler/mutable registry。
16. observation字段只来自本计划白名单 canonical source，不存在 mutable observation store双写。
17. local owner-lost不产生 recovery/replay/接管路径。
18. 首版不暴露跨 domain全局list/pagination API。
19. Workflow terminal delivery按 destination保留identity/revision。
20. progress是best-effort live signal，无伪连续sequence或durable history；stale/racing signal 不能推进 authoritative state，必须被后续 canonical snapshot 覆盖或丢弃。
21. Workflow cancel receipt不提前承诺CANCELLED；pause与effect/delivery IN_DOUBT不进入run terminal union。
22. canonical large terminal result只使用ArtifactRef；`task-output:`只允许明确local streaming log且不被renderer fallback解析。
23. wire decoder严格拒绝 malformed与cross-variant payload。
24. production composition只有Product唯一入口，无第二 factory/control path。
25. presentation、ACP、AG-UI、terminal不 import domain concrete owner。
26. 新旧 progress、terminal、submission receipt不存在双链。

## 12. 小批量测试矩阵

遵守“不运行大规模测试”的硬约束。按单文件或少量节点分批执行：

| 范围 | 必测行为 |
|---|---|
| identity/codec | canonical WorkflowRunReference nominal round-trip、async-work只包装/单次解包、strict negative fixtures、cross-variant rejection |
| local lifecycle | 同TaskId跨Pool不冲突，stale process/incarnation/attempt fail closed |
| local restart | 新Pool不接管旧reference，local observation不replay |
| Workflow CAS | cancel与settle/pause竞争，revision conflict，definition mismatch |
| Workflow access | create原子provenance/grant，principal/root containment/active incarnation/lineage revision/fence mismatch fail closed；creator lineage revision只用于审计 |
| Workflow recovery | Session close/resume不改grant，当前logical Agent lineage决定权限；creator terminal后ordinary Port失权但run fixed-continue |
| Workflow create admission | stable request/RunId/definition mapping，reserve后crash，run commit后settle前crash，旧owner未失权时reconciler不得abort，owner失权后claim唯一胜者，ABORTED同request重试typed reject，create与cancellation snapshot竞争只有一个合法结果 |
| durable migration | 旧run有证据时幂等生成provenance/grant/COMMITTED admission，缺失或冲突证据fail closed，partial migration重试不双写/双读 |
| root cancellation | frozen target/admission snapshot strict codec，acceptance/settlement分离，partial-intent crash scan，per-run idempotency，revision conflict retry，terminal/stale claim/fence settlement |
| progress | local/durable平级且互不嵌套冒充，commit/emit crash gap，racing stale signal不推进state且被snapshot覆盖/丢弃 |
| cancel | receipt只到CANCEL_REQUESTED，terminal settlement才产生CANCELLED |
| terminal | pause不进入terminal，ArtifactRef单真相，多destination identity，effect IN_DOUBT不冒充run phase |
| Product | typed submission/get/cancel，无裸ID/prefix guessing |
| presentation | local/durable badge、ID标签、available actions一致 |
| architecture | 分层、唯一owner、无第二registry/state/control/event链 |

完整 `ztest/architecture/` 按单文件或小组运行，不能一次性执行整个大目录；BackgroundTask、Workflow、presentation、ACP/AG-UI/terminal同样分组。交付报告逐项列出已运行节点与未运行的大规模范围。

## 13. 完成定义

只有同时满足以下条件才完成：

- 产品通过同一 strict union观察当前 Agent的两类异步工作；
- 每条 observation可由 kind定位唯一 authoritative owner；
- BackgroundTask仍严格 process-local，Workflow仍严格 durable；
- query/cancel/result/progress/terminal全链保留variant identity与并发条件；
- Workflow-as-task、Background包裹Workflow progress、混合facade、旧测试语义全部归零；
- Session/进程重启不恢复local execution，Workflow从canonical durable facts恢复；
- Workflow create admission 与 subtree cancellation snapshot 原子协调，frozen target/admission cutoff 使崩溃恢复不漏取消目标；
- root cancellation durable acceptance 与异步 settlement 严格分离，accepted intent 可由scan/query持续观测并仅经唯一 WorkflowRunControl 结算；
- Workflow canonical control/governance 不反向依赖 async-work observation identity，所有内部链使用 Workflow-owned `WorkflowRunReference`；
- RESERVED admission 能在 operation fence 下确定性结算 COMMITTED/ABORTED，不保存或盲重放 Workflow create payload；
- creator terminal 不产生Workflow mirror fact/receipt，available actions 只读投影为 creator unavailable/root-cancel-only；
- UI phase/action无法反向修改domain state；
- 无第二registry、第二状态机、第二command path、第二event链或observation双写；
- strict codec、类型检查、架构门禁和全部受影响小批量测试通过；
- R1.5、R2.9及canonical ledger机械计算的硬依赖闭包由当前生产证据重新签收，而非沿用历史DONE；其余工作包不因本计划被手工改写。

## 14. 明确不做

- 不让 BackgroundTaskPool跨进程恢复。
- 不把 WorkflowRun降级为 BackgroundTask。
- 不支持 local-to-durable promotion。
- 不提供首版跨Agent/root/application全局列表或分页。
- 不持久化local observation/reducer state。
- 不用统一status替代两个domain状态机。
- 不暴露 Pool map、Workflow store、reconciler、asyncio task或executor instance。
- 不保留 Workflow `task_id`、字符串 Workflow identity或旧wire compatibility path。
