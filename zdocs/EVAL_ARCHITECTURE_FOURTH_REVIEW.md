# Mote Eval 架构第四轮评审

> 评审对象：[`EVAL_PACKAGE_SKELETON.md`](./EVAL_PACKAGE_SKELETON.md)
>
> 前置评审：
>
> - [`EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md`](./EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md)
> - [`EVAL_ARCHITECTURE_SECOND_REVIEW.md`](./EVAL_ARCHITECTURE_SECOND_REVIEW.md)
> - [`EVAL_ARCHITECTURE_THIRD_REVIEW.md`](./EVAL_ARCHITECTURE_THIRD_REVIEW.md)
>
> 评审目标：逐状态、逐端口、逐 durable schema 审计跨组件原子性、并发终态竞争和恢复语义。

## 1. 结论

第四轮结论：**主设计仍缺少一份正式 Effect/Receipt 协议；在此之前，状态机无法安全驱动真实副作用。**

Eval 的 journal、worker、workspace、artifact、budget 和外部服务不处于同一个事务域。任何“先写 A、再调用 B”的实现都有崩溃窗口。不能用更多 try/finally 或单进程锁解决，也不能承诺通用 exactly-once。

正确的长期语义应是：

```text
durable intent
→ deterministic effect ID
→ idempotent start-or-attach
→ revisioned receipt
→ observation/reconcile
→ terminal fact
```

新增阻断项：

1. Reducer 只定义状态，没有定义由状态产生的 EffectCommand。
2. AttemptExecutor.start 不是 start-or-attach，恢复时无法安全重交。
3. journal 与 executor、workspace、artifact、budget 之间的双写窗口没有逐一分类。
4. cancel、timeout、worker success 竞争时没有唯一终态仲裁规则。
5. `in_doubt` 只出现在文字里，没有成为所有不可判定副作用的正式状态。
6. Artifact publication 与 CaseResultFinalized 缺少 publish-before-reference 和 reconcile 协议。
7. reducer version、decision policy version、effect protocol version 混在一起。
8. resume 在实现或配置升级后缺少 continue/fork/refuse 的正式判定。
9. “可复现”仍混合 auditability、replayability 和 rerun reproducibility 三种不同保证。
10. Eval 尚未给每个 durable schema 指定 authority、identity、revision 和 terminal invariant。

## 2. 不承诺 Exactly-Once

### 2.1 为什么不可承诺

以启动 Attempt 为例：

```text
写 AttemptScheduled 成功
→ 启动 worker 成功
→ 写 AttemptStarted 前 coordinator 崩溃
```

恢复后只能看到 AttemptScheduled。若直接重启，可能产生两个 worker；若不重启，又可能永久遗漏已运行 worker。

相反顺序同样错误：

```text
先写 AttemptStarted
→ 启动 worker 前崩溃
```

journal 会声称 worker 已启动，但真实副作用从未发生。

跨事务域不存在既写 journal 又启动进程的原子提交。

### 2.2 正式交付语义

Eval 应明确采用：

```text
事实提交：exactly-once per stream sequence，由 EventJournal 保证。

Effect 投递：at-least-once，允许相同 effect ID 重交。

Effect 执行：由目标端按 effect ID 和 request digest 幂等。

Effect 结果：revisioned receipt，可查询、可 reconcile。

最终结果：at-most-one terminal receipt；无法判断时进入 in_doubt。
```

任何端口不支持幂等 start-or-attach 或 reconcile，就不能作为 recoverable executor 使用，只能标记为 non-resumable。

## 3. Reducer 与 Effect Planner 必须分离

### 3.1 Reducer 不能执行 I/O

纯 reducer：

```python
reduce(state, envelope) -> state
```

它只能验证并投影事实，不能：

-启动 worker；
-创建 workspace；
-发布 artifact；
-预留 budget；
-调用 evaluator；
-发送 cancel。

### 3.2 增加 Effect Planner

```python
class EffectPlanner(Protocol):
    def plan(self, state: ExperimentState) -> tuple[EffectCommand, ...]: ...
```

`EffectCommand` 是可 codec 化、带确定 identity 的纯数据：

```python
@dataclass(frozen=True, slots=True)
class EffectCommand:
    effect_id: EffectId
    effect_type: str
    schema_version: int
    aggregate_id: str
    request_digest: ContentDigest
    fencing_token: int
    payload: JsonValue
```

运行循环：

```text
replay/reduce facts
→ planner 产生尚未满足的 effects
→ durable EffectIntentRecorded
→ dispatcher start-or-attach(effect)
→ receipt/outcome observation
→ durable EffectObserved/Terminal fact
→ reduce
```

同一 state 多次调用 planner 必须产生相同 effect IDs。Planner 不能使用当前时间或随机数；时间、seed 和 ID 必须在前序事实中已经固定。

### 3.3 Effect 类型

第一阶段至少包含：

```text
AcquireCoordinatorLease
MaterializeDataset
ReserveExperimentBudget
ReserveAttemptResources
MaterializeAttemptWorkspace
StartTaskAttempt
CancelTaskAttempt
ReclaimTaskAttempt
FreezeAttemptSnapshot
StartEvaluatorAttempt
CancelEvaluatorAttempt
PublishArtifact
SettleBudget
ReleaseWorkspace
BuildProjection
```

每种 effect 必须声明：authority、idempotency class、request digest、receipt schema、terminal states、reconcile 和 compensation 能力。

## 4. AttemptExecutor 必须是 Start-or-Attach

建议端口：

```python
class AttemptExecutor(Protocol):
    async def start_or_attach(
        self,
        request: AttemptExecutionRequest,
    ) -> AttemptExecutionReceipt: ...

    async def query(
        self,
        attempt_id: AttemptId,
        *,
        after_revision: int = 0,
    ) -> AttemptExecutionReceipt: ...

    async def cancel(
        self,
        request: AttemptCancellationRequest,
    ) -> AttemptExecutionReceipt: ...

    async def reconcile(
        self,
        request: AttemptReconcileRequest,
    ) -> AttemptExecutionReceipt: ...
```

约束：

- `attempt_id` 同时是 execution ID 和幂等键。
- 相同 attempt ID + 相同 request digest：返回已有 execution/receipt，不重复启动。
- 相同 attempt ID + 不同 request digest：返回 idempotency conflict。
- receipt revision 单调递增且不能跳回。
- fencing token 不能回退；旧 token 请求被拒绝。
- terminal receipt 不可转移。
- worker PID、socket 或内存对象不是 durable identity。
- query/reconcile 不要求原 coordinator 仍存活。

可参考 Mote inference 中已有的 AttemptReceipt、fencing token、request digest、revision、terminal 和 `IN_DOUBT` 模式，但不能直接复用 inference-specific 类型。

## 5. Attempt Receipt 状态机

建议：

```text
accepted
→ launch_intent_durable
→ launch_committed
→ worker_started_observed
→ task_started_observed
→ terminal_succeeded
 | terminal_failed
 | terminal_cancelled
 | terminal_timed_out
 | in_doubt
```

含义：

- `accepted`：executor 已接受 identity/request，尚未承诺启动。
- `launch_intent_durable`：executor 自己的 authority 已保存启动意图。
- `launch_committed`：启动副作用可能已经发生；此后普通失败不能安全当作“未启动”。
- `worker_started_observed`：已观察到 worker identity。
- `task_started_observed`：Product facade 已确认 task execution scope。
- terminal：唯一终态。
- `in_doubt`：启动/执行可能发生，但 authority 无法证明结果或完全回收。

`in_doubt` 必须是 terminal receipt，但不是成功、失败或取消。它阻止自动 retry，直到 recovery policy 用外部证据形成新的 reconciliation decision。不能把 in_doubt 降级成 failed。

## 6. 双写窗口清单

每一行都需要 durable intent、幂等 effect 和 reconcile：

| 操作 | 崩溃窗口 | 正确协议 |
| --- | --- | --- |
| journal → worker start | worker 已启动但无 started fact | deterministic AttemptId + start-or-attach + query |
| workspace create → journal | workspace 已存在但无 receipt | WorkspaceOperationId + manifest digest + adopt/reconcile |
| artifact publish → result fact | artifact 已发布但无引用 | idempotent publication ID；允许 orphan GC；重查 publication |
| result fact → artifact publish | durable fact 指向不存在 artifact | 禁止；必须 publish/verify 后才提交引用 |
| budget reserve → journal | reservation 占用但无 fact | deterministic reservation ID + query/release/reconcile |
| journal → budget reserve | scheduled 但未获得 budget | admission state 可恢复重试；不能启动 execution |
| cancel intent → executor | cancel 可能已生效但无 receipt | deterministic cancel ID + idempotent cancel/query |
| executor terminal → journal | execution 已终态但 experiment 未观察 | query/reconcile terminal receipt |
| journal completion → resource release | case 已完成但资源仍持有 | completion 前必须记录 release/reclamation receipt；后续 GC 另记 |
| projection write → projection fact | projection 已更新但无 observation | projection header 含 source sequence/digest；可重建覆盖 |

禁止设计依赖跨端口“两步都成功才算成功”的内存事务。

## 7. Artifact 发布顺序

任何 durable fact 引用 ArtifactRef 前必须满足：

```text
bytes 已在正确 sensitivity store 中持久化
→ publication request durable
→ ArtifactRevision committed
→ read/verify digest and size
→ journal fact 引用 ArtifactRef
```

如果 artifact 已发布但 journal commit 失败，它是合法 orphan，由 publication ID/reachability GC 对账。反方向不允许：journal 不能先保存未来可能出现的普通 ArtifactRef。

如果必须表示未完成发布，使用独立 `ArtifactPublicationIntentRef`，不能伪装成已可解析 ArtifactRef。

Artifact publication ID 建议由：

```text
effect ID + producer phase + representation
```

确定派生。重交同一 publication ID 但 bytes digest 不同是幂等冲突。

## 8. Workspace 操作也需要 Receipt

Workspace 不是随手创建的 Path，而是外部资源：

```python
class WorkspaceReceipt:
    operation_id: WorkspaceOperationId
    workspace_id: str
    source_snapshot_digest: ContentDigest
    materialization_policy_id: str
    state: prepared | materialized | frozen | released | failed | in_doubt
    revision: int
    fencing_token: int
    manifest_ref: ArtifactRef | None
```

规则：

- 相同 operation ID/source digest 重交必须 attach 已有 workspace。
- workspace 路径只存在于 infrastructure receipt 的本地 opaque locator，不进入 domain durable schema。
- freeze 产生 immutable tree manifest；之后 task writable lease 失效。
- evaluator overlays 从 frozen snapshot 新建，不复用 task writable workspace。
- release 是幂等操作；无法证明目录和挂载已释放时进入 in_doubt/unverifiable。
- orphan workspace 由 durable operation catalog 扫描，不靠目录名称猜测归属。

## 9. Budget 与 Resource Reservation Receipt

每个 reservation 使用确定 ID：

```text
experiment:<experiment_id>
attempt:<attempt_id>
evaluator:<evaluator_attempt_id>
```

状态：

```text
requested → reserved → partially_settled → settled
                    ↘ released
                    ↘ reconciliation_required
```

约束：

- reservation receipt durable 后才能启动对应 effect。
- executor terminal 不代表 usage 已结算。
- unknown usage 不能默认为 0。
- coordinator 恢复时先 reconcile 未结算 reservation。
- Experiment execution 可以 terminal，但 budget status 必须独立显示 reconciliation_required。
- 同一 usage receipt 使用 settlement ID 幂等，避免重复计费。

## 10. 完成、取消和超时竞争

### 10.1 单一终态 authority

AttemptExecutor receipt store 是 attempt execution terminal 的 authority。Experiment journal 只观察该 receipt，不自行制造相互矛盾的 terminal。

### 10.2 竞争规则

```text
success 先 terminal
  后续 cancel 返回 already_terminal(success)，不改状态。

cancel 先 terminal
  后到 success evidence 不改变 terminal；保存为 late evidence 并触发一致性告警。

timeout
  由唯一 deadline authority 发起 cancel/reclaim；不能由 coordinator 和 worker 各自独立提交 timed_out。

worker exit 与 cancel 同时发生
  executor 基于已持久 receipt/reclaim evidence 选择唯一 terminal；无法证明则 in_doubt。
```

“谁先被 asyncio 调度”不能决定 durable terminal；必须由 receipt CAS revision 决定。

### 10.3 Deadline authority

- ExperimentRunner 设定 policy deadline。
- AttemptExecutionRequest 固化 CrossProcessDeadline。
- AttemptExecutor 是 execution timeout 的唯一 authority。
- Product/worker 可以提前 cooperative timeout，但最终 terminal 仍由 executor receipt store 提交。
- wall-clock skew 或 daemon restart 后，根据 UTC deadline + remaining budget 对账；duration 仍使用 monotonic clock。

## 11. Fact Delivery 与 Ack

Worker 产生的事实不能直接竞争 experiment journal sequence。需要：

```python
class ExperimentFactGateway(Protocol):
    async def submit(self, candidate: FactCandidate) -> FactCommitAck: ...
    async def query(self, event_id: EventId) -> FactCommitStatus: ...
```

规则：

- candidate 有确定 event ID、causation ID 和 payload digest。
- gateway 验证 coordinator fencing token 和 aggregate transition。
- 相同 event ID/payload digest 重交返回原 ack。
- 相同 event ID/不同 payload digest 是冲突。
- ack 只在 EventJournal durable append 后返回。
- worker 超时未收到 ack 时重交，不生成新 event ID。
- coordinator 不信任 worker 自报 terminal；必须与 executor receipt/artifact evidence 对账。

## 12. State、Decision、Effect、Receipt 四层模型

建议正式分层：

```text
Fact
  已发生且 durable 的不可变事实。

State
  reducer 从 facts 得到的确定投影。

Decision
  versioned policy 根据 State 选择下一业务决策，例如 retry/skip/select。

Effect
  为实现 Decision 而请求外部 authority 执行的幂等命令。

Receipt
  外部 authority 对 Effect 的 revisioned、可 reconcile 结果。
```

版本必须分开记录：

- event payload schema version；
- reducer version；
- decision policy ID/version；
- effect command schema version；
- receipt schema version；
- projection schema version。

迁移 event schema 不得偷偷改变旧实验的 retry/selection decision。历史 decision 必须作为事实保存，不能用当前 policy 重新计算后覆盖。

## 13. Retry 决策必须持久化

`RetryScheduled` 不能只是 reducer 根据 attempt failure 临时推导。它必须保存：

```text
decision_id
policy_id/version/config_digest
source_attempt_id
failure classification
side-effect/reclamation evidence refs
budget reservation status
next_attempt_id
seed policy
decision reason
```

恢复时读取已提交 RetryScheduled，而不是用升级后的 retry policy 再判断一次。

同理，AttemptSelected、EvaluatorSkipped、CaseAborted 和 ExperimentCancelled 都是 durable decision facts。

## 14. Resume、Fork 与 Upgrade

### 14.1 Resume

Resume 只能在以下条件满足时继续同一 ExperimentId：

- durable schema decoder 可用；
- reducer version 能处理现有 facts；
-冻结的 ExecutionManifest 实现和 request digests 可解析；
-当前 host capability 满足原 execution plan；
- coordinator fencing lease 获得；
-未终态 effects 可 query/reconcile；
-安全 policy 没有被降级。

### 14.2 Refuse

缺少旧 Task/Evaluator executable、executor 不支持 reconcile、fixture/artifact 损坏或权限策略不兼容时，应拒绝 resume。仍允许 show/verify/migrate。

### 14.3 Fork experiment

要使用新代码、模型、policy、dataset 或 evaluator 继续，应创建新 ExperimentId：

```text
parent_experiment_id
fork_reason
source_sequence
inherited materialized dataset refs
new provenance/manifest
```

Fork 不继承旧 experiment 的非终态 execution lease，也不伪装成 resume。

## 15. 三种“复现”必须分开

### 15.1 Auditability

能够解释当时发生了什么：请求摘要、模型/provider observation、tool/effect receipts、metrics 和 artifacts 完整。

### 15.2 Replayability

不重新调用外部系统，仅从 journal/artifacts 重建相同 ExperimentState 和 report。

### 15.3 Rerun reproducibility

重新执行 task/evaluator，期望在定义的容差内得到等价结果。

LLM、搜索、外部 API 和系统时间通常使 exact rerun 不可能。Provenance grade 不应把“本地输入有 digest”称为 exact rerun。

建议分别给出：

```text
audit_grade
replay_grade
rerun_grade
```

模型交互需要记录：

- canonical request digest；
-实际 provider/model/route/generation；
-响应或受控 response artifact digest；
- usage/cost receipt；
- toolset/system prompt/skill manifest；
- redaction/retention policy。

若因隐私不保存响应内容，只能降低 audit/replay grade，不能声称 exact。

## 16. Durable Schema Authority 表

实现前主设计必须为每个 schema 补齐下表：

| Schema | Authority | Identity | Revision | Terminal invariant |
| --- | --- | --- | --- | --- |
| Experiment journal | EventJournal | StreamId | sequence | append-only |
| Coordinator lease | Lease authority | ExperimentId | fencing token | expired/released |
| Attempt receipt | AttemptExecutor store | AttemptId | receipt revision | one terminal |
| Evaluator receipt | EvaluatorExecutor store | EvaluatorAttemptId | receipt revision | one terminal |
| Workspace receipt | Workspace authority | OperationId | revision | frozen/released/in_doubt |
| Artifact revision | ArtifactStore | artifact ID + revision | artifact revision | immutable bytes |
| Budget reservation | Budget authority | ReservationId | revision | settled/released/reconcile |
| CaseResultFact | Experiment journal | CaseId + RunId | event sequence | immutable digest |
| Projection | Projection store | ExperimentId + projection kind | source sequence | replaceable/rebuildable |

没有 authority 或 revision 的“receipt”只是普通返回值，不能承担恢复语义。

## 17. Failure Classification

建议统一：

```text
pre_effect_failure
  可证明副作用未提交，可按 policy retry。

effect_rejected
  authority 明确拒绝，通常非 retry 或等待 admission。

effect_failed
  authority 明确 terminal failed，依据 side-effect receipt 判断 retry。

effect_cancelled
  terminal cancelled，仍需 reclamation/usage settlement。

effect_timed_out
  deadline authority 已提交 terminal timed out。

effect_in_doubt
  可能已发生副作用但无法证明结果；禁止自动 retry。

protocol_conflict
  identity/request/fencing/revision 冲突；实验进入 blocked。

integrity_failure
  journal/artifact/schema evidence 损坏；fail-closed。
```

Python exception class只作为 evidence，不直接决定 durable failure class。

## 18. Recovery Decision Matrix

| Journal state | Receipt state | Recovery action |
| --- | --- | --- |
| intent absent | execution absent | 不执行，重新由 planner 决定 |
| intent present | execution absent | start-or-attach 同一 effect ID |
| intent present | non-terminal | attach/query，必要时 cancel/reconcile |
| intent present | terminal | 提交 observation/terminal fact |
| terminal fact present | receipt same terminal | 已完成，无动作 |
| terminal fact present | receipt conflict | integrity/protocol blocked |
| intent present | receipt missing but launch committed evidence | in_doubt/reconcile，禁止盲目重启 |
| cancel intent present | execution terminal success | 记录 cancel no-op/already terminal |
| timeout intent present | execution non-terminal | deadline authority cancel/reclaim |

该矩阵需要覆盖每一种 Effect 类型，而不是只覆盖 task attempt。

## 19. Crash Test Matrix

每个 Effect 至少在以下位置注入进程级崩溃：

```text
before intent commit
after intent commit / before dispatch
after target accepted / before receipt persisted
after receipt persisted / before response
after response / before experiment observation
after terminal artifact publish / before terminal fact
after terminal fact / before resource release
after release / before release observation
```

并发竞争测试：

- success vs cancel；
- success vs timeout；
- cancel vs timeout；
- coordinator A vs coordinator B；
-旧 fencing token vs 新 token；
-相同 ID/不同 request digest；
- worker terminal vs network partition；
- artifact published vs disk full；
- budget settled vs coordinator crash。

验收标准不是“不报错”，而是：无重复不可控副作用、无非法终态转换、无预算重复结算、无 dangling durable ref，并能明确进入 completed 或 in_doubt/blocked。

## 20. 新增实现前门槛

在前三轮门槛基础上增加：

1. 文档明确不承诺跨 authority exactly-once。
2. 所有副作用遵循 durable intent + deterministic effect ID + receipt + reconcile。
3. reducer、decision policy、effect planner 和 dispatcher 职责分离。
4. AttemptExecutor 使用 start-or-attach，而不是不可重交的 start。
5. 相同 execution ID/不同 request digest 必须产生幂等冲突。
6. 每个 receipt 有 authority、revision、fencing token 和不可变 terminal。
7. `in_doubt` 是正式终态分类，并阻止自动 retry。
8. journal 不允许引用尚未 committed/verified 的 ArtifactRef。
9. workspace、artifact、budget、cancel 和 projection 的双写窗口均有 reconcile 协议。
10. completion/cancel/timeout 由 receipt CAS 仲裁唯一终态。
11. timeout 只有一个 execution authority。
12. worker fact delivery 使用确定 event ID，durable ack 前允许原 ID 重交。
13. retry/selection/skip/abort/cancel policy decision 都作为事实持久化。
14. resume、refuse 和 fork experiment 有确定判定。
15. audit、replay、rerun 三种 reproducibility grade 分开。
16. 每个 durable schema 都有 authority/identity/revision/terminal invariant。
17. failure classification 不依赖异常字符串或 Python 类名猜测。
18. recovery matrix 覆盖每个 Effect 类型。
19. crash tests 覆盖每个双写边界和终态竞争。
20. 所有无法证明的结果进入 in_doubt/blocked，不伪装为 failed 后自动重试。

## 21. 最终判断

经过四轮评审，Eval 的最终核心已经不再是 `DatasetRunner`，而是一个小型、严格的 durable effect system：

```text
Facts → Reducer → State → Versioned Decisions
      → Durable Effect Intents → Idempotent Authorities
      → Revisioned Receipts → Reconciliation → Facts
```

Dataset、Evaluator 和 Report 都建立在这个闭环之上。

下一步应停止新增评审分支，先将四轮结论合并成主设计的 canonical state/effect/receipt 规格，并删除与现有 Mote Event/Artifact 基础设施重复的内容。合并完成后，再做一次最终的“最小可交付范围”削减评审，防止 Phase 1 同时实现一个过大的分布式控制面。
