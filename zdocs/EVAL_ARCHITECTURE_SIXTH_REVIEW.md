# Mote Eval 架构第六轮评审：Canonical 合并决策

> 评审对象：Eval 主设计与前五轮评审。
>
> 评审目标：裁决评审意见之间的冲突，确定 canonical 主设计重写时每个类型、端口、实现和依赖的唯一归属。

## 1. 结论

五轮评审已足够覆盖方向、恢复、隔离、安全和首版范围。当前最大的风险不再是遗漏，而是多份文档同时包含互相冲突的方案。

第六轮给出合并裁决：

1. 采用第五轮的最小 Phase 1，不采用主设计当前的“大而全 Phase 1”。
2. 采用第四轮的 Fact/State/Decision/Effect/Receipt 模型，但 Phase 1 使用封闭 effect union，不做通用工作流平台。
3. 采用第二轮的 Mote 通用 Event/Artifact 契约复用结论。
4. 采用第三轮的 coordinator fencing、资源 admission、evaluator overlay 长期设计，但 Phase 1 只实现能力受限的单机版本。
5. Product 只提供 Agent/Product 能力，不得反向 import 或声明 Eval 类型。
6. Eval 对 Runtime 的复用通过一个经 ADR 批准的通用 durability facade 完成，不允许自由 import runtime 内部模块。
7. canonical 主设计重写后，旧主设计和五份评审都降级为历史审计材料；实现只能引用 canonical 文档。

## 2. 最终依赖方向

```text
contracts
  ↑
runtime public durability facade
  ↑
eval/adapters/local

contracts ← product public headless facade
               ↑
        eval/adapters/mote

eval/domain ← eval/application ← eval/composition
     ↑              ↑                  ↑
contracts DTO   contracts ports   local + mote adapters
```

规则：

- `eval/domain` 依赖 stdlib 和批准清单中的 Contracts DTO。
- `eval/application` 依赖 domain 和批准清单中的 Contracts ports。
- `eval/adapters/mote` 只依赖 eval domain/application、Contracts 和 Product 公开 facade。
- `eval/adapters/local` 只依赖 eval domain/application、Contracts 和一个 Runtime 公开 durability facade。
- `eval/composition` 负责组装 local 与 mote adapters。
- `eval/reporting` 只依赖 domain/query views。
- `eval/cli` 只调用 composition/application/reporting。
- Product、Runtime、Orchestration、Kernel、Contracts 均不得 import Eval。
- Eval 其他模块不得 import Runtime 或 Product。

## 3. 为什么不能让 Product 提供 EvalJournalFactory

Product 位于 Eval 下方，不能声明以下接口：

```python
def open_eval_journal(experiment_id: ExperimentId) -> ...
```

否则 Product 必须认识 Eval identity 或语义，形成反向依赖。

正确方式是 Product/Runtime 提供通用能力：

```python
class LocalDurabilityFacade(Protocol):
    def open_event_journal(self, stream_id: StreamId) -> EventJournal: ...
    def open_artifact_scope(self, ownership: ArtifactScopeRequest) -> ArtifactScope: ...
    def atomic_projection_store(self, namespace: str) -> ProjectionStore: ...
```

Eval local adapter 负责映射：

```text
ExperimentId → StreamId
ExperimentId/AttemptId → Artifact ownership
ExperimentId → projection namespace
```

Runtime facade 不知道这些 ID 的业务含义，只验证通用字符串 identity、scope 和路径安全。

## 4. Runtime Durability Facade 的边界

### 4.1 允许暴露

- EventJournal port 的本地 factory；
- scoped ArtifactStore/Resolver/Publisher；
-原子 projection store；
-安全 namespace/path layout；
- file lock/fencing generation primitive；
- DiskWriter/lifecycle close 的高层封装。

### 4.2 禁止暴露

- SessionLog；
- RoleState；
- ToolExecutor；
- PermissionEngine 实例；
- AgentControl；
- ContextManager；
- runtime 私有 repository/layout/path；
-可绕过 policy 的 subprocess/terminal；
-裸 CAS filesystem path。

Eval adapter 只能消费 facade/Contracts port，不能向下穿透。

### 4.3 ADR 要求

必须通过 ADR 固化：

- public module path；
-稳定性等级；
-所有权与 close 语义；
-允许的 Eval import 白名单；
-禁止的 runtime import 测试；
-未来 local implementation 迁移不影响调用方。

## 5. Product Headless Facade 的唯一职责

Product facade 只解决“非交互式运行一个 Product Agent application”：

```python
class HeadlessCodingApplication(Protocol):
    async def execute(self, request: CodingExecutionRequest) -> CodingExecutionReceipt: ...
    async def cancel(self, request: CodingCancellationRequest) -> CodingExecutionReceipt: ...
    async def aclose(self) -> CodingCleanupReceipt: ...
```

Product DTO 不包含：

- ExperimentId；
- Dataset/Case；
- Eval metric；
- Eval journal sequence；
- retry/selection policy；
- Eval report。

它只包含通用 execution/session identity、prompt/turns、cwd capability、model route、headless interaction policy、resource limits 和公开 receipt。

Eval Mote adapter 将 AttemptExecutionRequest 映射为 Product CodingExecutionRequest。

CLI 也应复用该 facade，避免第二套 Coding Agent composition。

## 6. Product Command Service 的归属裁决

`PolicyBoundCommandService` 属于 Product 公共能力，因为它封装：

- Product permission configuration；
- Tool policy pipeline；
- sandbox；
- headless interaction policy；
- command runtime；
- artifact output。

它不能放在 Eval，也不能直接暴露 Runtime PermissionEngine。

Phase 1 不实现 CommandEvaluator，因此 Product Command Service 可以作为 Phase 2 前置，不阻塞 Phase 1。

## 7. Event 契约裁决

### 7.1 删除

canonical 主设计删除自定义 `EvalEventEnvelope`。

### 7.2 复用

使用：

- `contracts.events.EventEnvelope`；
- `contracts.ports.events.EventJournal`；
- `UncommittedFact`；
- `AppendResult`；
- `VerificationReport`。

### 7.3 Eval 自有

Eval 只定义：

- namespaced EventType 常量；
- versioned fact payload DTO；
- payload codec；
- reducer/state；
- recovery decisions。

### 7.4 Torn tail

采用第二/第四轮严格策略：

- verify fail-closed；
- Phase 1 无 repair 命令；
-损坏实验只能 show verified prefix diagnostic，不能 resume；
- repair 后置，且必须留存原 journal artifact 与 repair fact。

## 8. Artifact 契约裁决

### 8.1 删除

canonical 主设计删除 Eval 自定义通用 ArtifactRef、Digest、Retention 和 Sensitivity。

### 8.2 复用

使用 Contracts：

- ArtifactRef；
- ArtifactRevision；
- ArtifactRetention；
- ArtifactSensitivity；
- ArtifactResolutionPolicy；
- ContentDigest/ContentIdentity。

### 8.3 Eval 自有

```python
class EvalArtifactRecord:
    artifact: ArtifactRef
    experiment_id: ExperimentId
    case_id: CaseId | None
    run_id: RunId | None
    attempt_id: AttemptId | None
    producer_phase: str
    producer_id: str
```

artifact 当前完整性属于可变 health projection，不进入不可变 CaseResult digest。

### 8.4 Ownership 映射

Phase 1：

```text
SESSION owner = attempt_id
PROJECT owner = experiment artifact owner ID
PINNED = 不自动使用
```

进入 CaseResult/report 的 artifact 在 journal 引用前必须已发布为 PROJECT 可达。未选中/临时 attempt artifact 使用 SESSION，attempt 完成后按 policy release。

## 9. Executor 命名与能力裁决

删除 `CaseExecutor`，统一为 `AttemptExecutor`。

Phase 1 实现：

```text
LocalEphemeralProcessAttemptExecutor
```

明确 capability：

```text
restart_attach = false
reconcile_after_owner_loss = false
untrusted_code_isolation = false
automatic_retry_safe = false
```

长期 Phase 2 实现新的：

```text
DurableLocalProcessAttemptExecutor
```

后者必须有独立 supervisor/receipt authority，不能通过给 Ephemeral executor 增加几个 if 语句冒充。

## 10. Coordinator 裁决

长期 port 使用 `ExperimentCoordinatorAuthority` 和 fencing token。

Phase 1 local 实现：

- OS exclusive lock；
- durable fencing generation；
- owner process identity；
- mutation 前 assert current；
-不支持远程 TTL/renewal。

第二个 coordinator fail-fast。不能通过 EventJournal expected version 代替 coordinator authority。

## 11. Case/Attempt 状态机裁决

### 11.1 CaseMachine

```text
declared
→ started
→ attempt_pending
→ attempt_terminal
→ snapshot_pending
→ evaluating
→ finalizing
→ completed | aborted | blocked
```

### 11.2 AttemptMachine

```text
scheduled
→ workspace_pending
→ start_intent_durable
→ accepted/running
→ task_terminal
→ teardown_observed
→ reclamation_observed
→ snapshot_frozen
→ finalized
```

execution、teardown、reclamation 保持正交字段，不强塞进单一 enum。

### 11.3 Phase 1 限制

-每个 Run 只有一个 Attempt；
-无 RetryScheduled；
- coordinator 丢失活动 attempt 后进入 in_doubt/blocked；
- evaluator 只在 task succeeded + snapshot frozen 后运行；
- CaseCompleted 必须在 evaluator 结束和 cleanup/reclamation 已观察后提交。

## 12. Effect/Receipt 裁决

采用第四轮模型，但 Phase 1 不公开通用 Effect API。

内部封闭 effects：

```text
MaterializeWorkspace
StartAttempt
CancelAttempt
ReclaimAttempt
FreezeSnapshot
PublishArtifact
RunEvaluator
BuildProjection
```

每个 effect：

-确定 ID；
- request digest；
- durable intent；
- authority；
- receipt；
- terminal/in_doubt；
- reconcile capability declaration。

Phase 1 executor 不支持 owner-loss reconcile，所以恢复活动 StartAttempt 时直接 blocked/in_doubt，不重交 start。

## 13. Lifecycle 裁决

删除 case 外层单一 `CaseLifecycle` 的含混设计，改成所有权明确的三个阶段：

```text
AttemptProvisioner
  fixture → writable attempt workspace receipt

ProductExecutionLifecycle
  Application/Agent/task execute + cooperative teardown receipt

AttemptReclaimer
  parent-owned force reclaim + verification receipt
```

snapshot freeze 属于 Workspace authority，不属于 Product lifecycle。

Evaluator 使用 immutable snapshot；Phase 1 只读，不创建 overlay。Phase 2 CommandEvaluator 再加入 EvaluatorWorkspace authority。

## 14. Retry/Repeat/Compare 裁决

Phase 1：

```text
repeat = 1
task attempts = 1
evaluator attempts = 1
compare = unsupported
```

保留 RunId/AttemptId/EvaluationId，但不暴露大于 1 的配置。

Phase 2：retry。

Phase 3：repeat 和 compare。

Best-of-N 不属于 retry 或 repeat，必须未来独立 ADR。

## 15. Codec 与 Canonical Digest 裁决

复用 Contracts JsonValue/ContentDigest，但 Eval 定义 `CanonicalValueV1`。

Phase 1 支持：

- None/bool/int/string；
- tuple/list；
- string-key mapping；
-显式 codec 化 dataclass。

Identity/config digest boundary 暂不接受 float、Decimal、datetime、Path 和 bytes。它们必须通过显式字符串/Artifact codec。

CanonicalValueV1 必须有：

- NFC Unicode；
- UTF-8；
- sorted keys；
- compact separators；
- signed 64-bit int；
- format ID + codec ID/version 前缀；
- golden vectors。

Metric scalar float 可以作为 observation，但不直接参与 config/identity canonical digest；MetricObservation codec 单独规定有限 float encoding。

## 16. Provenance 裁决

采用第五轮最小 provenance。

三种等级分别记录：

- audit grade；
- replay grade；
- rerun grade。

禁止：

-裸 secret hash；
-未脱敏 diff；
-绝对路径进入 digest；
-仅凭 model alias 声称 exact；
-将 telemetry 日志当 durable provenance。

Phase 1 缺失 dependency lock 或外部 response artifact 时降低 grade，不阻止运行。

## 17. Schema 与版本裁决

每个 durable DTO 从 v1 开始有：

- schema owner；
- identity；
- exact field validation；
- decoder；
- golden fixture；
- unknown field policy；
- size limits。

Phase 1 不实现 migration runner，但代码结构必须分离 ExecutionRegistry 和 SchemaDecoderRegistry。

旧实验 show/replay 不能加载 Task/Evaluator executable。

## 18. Storage 裁决

Runtime durability facade 接收通用 validated namespace，不接受 display name 或任意 Path。

EvalStorageLayout 逻辑结构由 Eval local adapter定义：

```text
experiments/<experiment-id>/journal
experiments/<experiment-id>/projections
experiments/<experiment-id>/locks
workspaces/<attempt-id>/...
```

物理 root 由 RuntimePaths/facade 注入。

规则：

-路径 segment 使用经过验证的稳定 ID；
-不暴露 physical path 为 public API；
-0700/0600；
- symlink fail-closed；
- quota；
-删除/GC 后置，不在 Phase 1 提供递归 delete CLI。

## 19. Canonical 包结构

```text
eval/
├── __init__.py
├── domain/
│   ├── ids.py
│   ├── dataset.py
│   ├── codecs.py
│   ├── metrics.py
│   ├── provenance.py
│   ├── artifacts.py
│   ├── facts.py
│   ├── state.py
│   └── results.py
├── application/
│   ├── ports.py
│   ├── requests.py
│   ├── runner.py
│   ├── planner.py
│   ├── reducer.py
│   └── queries.py
├── adapters/
│   ├── local/
│   │   ├── durability.py
│   │   ├── coordinator.py
│   │   ├── executor.py
│   │   └── workspace.py
│   └── mote/
│       ├── application.py
│       ├── coding_task.py
│       └── evaluators.py
├── reporting/
│   ├── projection.py
│   ├── json_report.py
│   └── terminal.py
├── composition.py
└── cli/
    └── __main__.py
```

不再保留含义模糊的 generic `infrastructure/` 大包；按 authority 分入 local adapter。通用纯逻辑留在 domain/application。

## 20. Canonical 文档必须删除的旧内容

重写主设计时删除：

- EvalEventEnvelope；
- Eval 通用 ArtifactRef/Digest/Retention/Sensitivity；
- CaseExecutor；
- CaseLifecycle 单一 hook 协议；
- task retry/evaluator retry 首版承诺；
- CommandEvaluator 首版承诺；
- balanced/relaxed journal durability；
-自动 torn-tail 忽略；
- heartbeat journal events；
- remote/sandbox/dataset evaluator 首版内容；
- compare 首版内容；
-动态 plugin/migration runner 首版内容；
- Product EvalJournalFactory；
-活动 attempt crash-safe resume 承诺。

## 21. Canonical 文档必须新增的内容

- Runtime public durability facade ADR；
- Product public headless application facade；
- approved Contracts import allowlist；
- AttemptExecutor capability negotiation；
- coordinator authority/fencing；
- CaseMachine/AttemptMachine；
- Product teardown vs executor reclamation；
- immutable snapshot；
- Fact/State/Decision/Effect/Receipt；
- in_doubt/blocked；
- authority/identity/revision/terminal invariant 表；
- audit/replay/rerun grades；
- Phase 1 unsupported capability manifest；
-最小 CLI/API surface；
-双写与 crash test matrix。

## 22. 合并后的文档治理

建议：

```text
EVAL_PACKAGE_ARCHITECTURE.md
  唯一 canonical specification。

EVAL_PACKAGE_IMPLEMENTATION_PLAN.md
  Phase 0/1 可执行计划。

EVAL_ARCHITECTURE_REVIEW_LOG.md
  汇总六轮结论及关闭状态。
```

当前 `EVAL_PACKAGE_SKELETON.md` 在 canonical 文档完成后标记 superseded，不继续原地作为多义讨论稿。

六份独立评审保留为审计证据，但实现 PR 不能引用评审中的冲突方案作为规格。

## 23. 最终判断

现在已经具备重写 canonical specification 所需的全部关键决策，不建议再进行第七轮发散式问题搜寻。

下一步正确动作是：

1. 按本决策矩阵重写唯一主设计；
2. 建立 review closure 表，逐条映射六轮问题；
3. 再从 canonical 文档生成 Phase 0/1 implementation plan；
4. 最后做一次只检查“文档内部自洽与计划可执行性”的终审。

第六轮之后继续新增平行评审文档，会重新制造文档治理负债，不符合十年零负债目标。
