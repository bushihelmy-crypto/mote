# Mote Eval 架构第五轮评审：最小可交付范围削减

> 评审对象：[`EVAL_PACKAGE_SKELETON.md`](./EVAL_PACKAGE_SKELETON.md)
>
> 前置评审：第一至第四轮 Eval 架构评审。
>
> 评审目标：在不破坏十年稳定边界的前提下，将 Phase 1 削减为能够验证核心架构的最小纵向闭环，避免一次实现分布式控制面、沙箱平台、统计平台和插件平台。

## 1. 结论

前四轮提出的长期契约大体必要，但如果全部进入 Phase 1，首版会同时建设：

- durable workflow engine；
-进程 supervisor；
-分布式 lease/fencing；
-资源 admission；
- artifact 安全存储；
- sandbox command service；
-插件与 schema migration 平台；
-统计比较平台。

这会使 Eval 在尚未完成第一个真实 case 前就拥有过大的实现面，并极易产生“协议齐全、行为未证实”的新负债。

第五轮建议：

> 长期端口从第一版存在，但 Phase 1 只实现单机、单 coordinator、无自动 retry、无 evaluator retry、无命令 evaluator、无活动 execution 恢复的最小闭环。无法安全恢复的活动 attempt 明确进入 `in_doubt`，绝不伪装成 crash-safe。

这不是降低长期标准，而是用诚实的 capability boundary 避免伪实现。

## 2. Phase 1 的唯一目标

Phase 1 只证明以下纵向链路：

```text
materialized Dataset
→ strict experiment journal
→ pure reducer
→ single-host coordinator ownership
→ fresh workspace
→ one local-process Mote Coding attempt
→ immutable workspace snapshot
→ trusted read-only evaluators
→ typed metrics
→ teardown/reclamation receipts
→ immutable CaseResultFact
→ replayable JSON/terminal report
```

验收对象不是吞吐量或功能数量，而是：

-事实与投影一致；
-进程正常/失败/取消路径资源闭合；
- journal replay 稳定；
- artifact/metric/provenance identity 稳定；
- crash 后不会盲目重复产生副作用；
-现有 Mote 五层没有形成反向依赖或第二套装配路径。

## 3. Phase 1 必须实现

### 3.1 Domain

- ExperimentId、DatasetId、CaseId、RunId、AttemptId、EvaluatorId、MetricId；
- MaterializedDataset 和 Case；
- JsonValueCodec 及 CanonicalValueV1；
- MetricDefinition/MetricObservation；
- Eval fact payload union；
- CaseMachine + AttemptMachine 状态；
- pure reducer；
-不可变 CaseResultFact；
-最小 provenance；
- EvalArtifactRecord，复用 Contracts ArtifactRef。

### 3.2 Application

- ExperimentRunner；
- EffectPlanner 的最小实现；
- AttemptExecutor port；
- WorkspaceSnapshot port；
- ExperimentJournal/Artifact host ports；
- coordinator ownership port；
- cancellation scope；
- show/verify 查询。

### 3.3 Infrastructure

- existing EventJournal port 的本地注入；
- strict fsync，不提供 durability 配置；
-单机独占 coordinator lock；
- LocalEphemeralProcessAttemptExecutor；
- fixture materializer；
- fresh attempt workspace；
- immutable tree snapshot；
-原子 JSON projection；
-受信任、只读 evaluator executor。

### 3.4 Mote adapter

- Product headless Coding Application facade；
- MoteCodingTask；
-单轮 prompt；
-公开 RunOutcome/session/cost/artifact receipt；
- ask→deny；
-正常关闭和父级强制回收。

### 3.5 Reporting/CLI

- `run`；
- `show`；
- `verify`；
- JSON report；
-终端表格；
-不提供 compare/resume active execution/repair。

## 4. Phase 1 明确不实现

### 4.1 自动 Retry

Phase 1：

```text
max_attempts = 1
```

原因：自动 retry 依赖外部副作用证明、fresh attempt、预算、reclamation 和 selection policy 的完整闭环。先实现字段和 schema 会制造未验证语义。

长期模型仍保留 Run/Attempt 分层。第二阶段加入 retry 时不需要迁移 identity 或 result schema。

### 4.2 Evaluator Retry

每个 evaluator 只运行一次。失败记录 EvaluationResult failed，不重试。

长期保留 EvaluationId/EvaluatorAttemptId 设计，但 Phase 1 不必公开配置 max evaluator attempts。

### 4.3 CommandEvaluator

没有 Product PolicyBoundCommandService 和 evaluator overlay 前，不实现命令 evaluator。

首版 evaluators：

- FileExists；
- FileContent；
- Json；
- ChangedFiles；
- TextMatch。

它们只消费 immutable WorkspaceView。

### 4.4 Remote/Sandbox Executor

不实现 remote、container 或不可信插件执行。

Local executor 必须公开 capability：

```text
live_cancel = supported
live_force_reclaim = supported
restart_attach = unsupported
cross_host_reconcile = unsupported
untrusted_code = unsupported
```

### 4.5 活动 Attempt 的 Crash Resume

coordinator 重启后：

-已 terminal 且 receipt/facts 完整的 case 正常 replay；
-尚未启动的 scheduled case 可继续；
-处于 launch committed/running 但 executor 无法 attach 的 attempt 标记 `in_doubt`；
-默认停止实验，需要显式 abort/fork；
-禁止自动重启同一或下一 attempt。

这诚实反映 LocalEphemeralProcessAttemptExecutor 的能力。

### 4.6 Dataset Evaluator、LLM Judge 和浏览器评测

全部后置。第一阶段不为它们扩展协议或添加空实现。

### 4.7 Compare 与高级统计

首版只报告原始 observations、failure/missing counts 和简单声明聚合。不输出显著性、置信区间或跨实验优劣结论。

### 4.8 动态插件

Task/Evaluator/Codec 使用代码内显式注册表并在启动时冻结。不加载 entry points、任意 import path 或网络插件。

### 4.9 Schema Migration 执行器

所有 durable schema 从第一版带版本，decoder 严格校验。Phase 1 只支持版本 1，不实现“为了未来而空转”的 migrator framework。

要求预留独立 decoder seam 和 fixture golden files；出现真实 v2 时再通过 ADR 实现 v1→v2 migrator。

## 5. LocalEphemeralProcessAttemptExecutor

### 5.1 为什么命名必须诚实

它不是 durable supervisor。它只能在 coordinator 存活时：

-启动 worker；
-观察 worker；
- cooperative cancel；
- kill process group；
-回收资源；
-返回 terminal receipt。

coordinator 崩溃后，它不能可靠 attach 原 worker，因此不得命名为 `DurableLocalAttemptExecutor`。

### 5.2 Port capability

```python
@dataclass(frozen=True, slots=True)
class AttemptExecutorCapabilities:
    live_cancel: bool
    live_force_reclaim: bool
    restart_attach: bool
    reconcile_after_owner_loss: bool
    untrusted_code_isolation: bool
```

Experiment plan 在启动前验证所需能力。请求不能依赖 executor 未声明支持的语义。

### 5.3 Phase 1 Receipt

仍需 revisioned receipt 和 request digest，但状态可以削减为：

```text
accepted
launch_committed
running
terminal_succeeded
terminal_failed
terminal_cancelled
terminal_timed_out
in_doubt
```

不实现无实际证据来源的细粒度状态。

### 5.4 父级回收

首版必须做到：

-新 process group/session；
- worker identity receipt；
- cooperative cancellation grace period；
- process group kill；
-等待退出；
-检查已知子进程；
- cleanup/reclamation 状态分离；
-无法证明时 `unverifiable`，不记 succeeded。

## 6. Coordinator 所有权的最小实现

长期是 lease + fencing，Phase 1 可使用单机独占锁，但必须通过同一 port：

```python
class ExperimentCoordinatorAuthority(Protocol):
    async def acquire(self, experiment_id: ExperimentId) -> CoordinatorLease: ...
    async def assert_current(self, lease: CoordinatorLease) -> None: ...
    async def release(self, lease: CoordinatorLease) -> None: ...
```

本地实现要求：

- OS file lock，而不是仅创建 lock 文件；
-锁文件保存 owner PID、process start identity、acquired time 和 fencing generation；
-所有 mutation 前 assert lease current；
-第二个 runner fail-fast，不等待后静默接管；
-进程退出由 OS 释放锁；
- fencing generation durable 单调递增；
-路径来自安全 EvalStorageLayout。

Phase 1 不实现 TTL/remote renewal，但 port 不使用“file lock”术语，避免未来破坏调用方。

## 7. Effect System 的最小范围

Phase 1 不需要通用动态 Effect 插件系统。只需封闭 union：

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

每个 effect 有确定 ID 和 request digest。Planner 是内部纯函数，不作为首版公共 API。

不需要：

-通用 workflow DSL；
-任意 effect registry；
-跨 host dispatcher；
-通用 compensation graph；
-用户自定义 effect。

如果未来 orchestration workflow 能完全承担该语义，应通过 ADR 合并，而不是让 Eval effect system 演化成第二个通用 workflow engine。

## 8. Journal 事件的最小闭包

首版事实集应足够恢复但避免逐函数打点：

```text
ExperimentDeclared
ExperimentStarted
CaseDeclared
CaseStarted

WorkspaceMaterializationIntent
WorkspaceMaterialized

AttemptStartIntent
AttemptReceiptObserved
AttemptTerminalObserved
AttemptReclamationObserved
AttemptSnapshotFrozen

EvaluatorStarted
EvaluatorCompleted
EvaluatorFailed
EvaluatorSkipped

CaseResultFinalized
CaseCompleted
CaseAborted

ExperimentCompleted
ExperimentCancelled
ExperimentBlocked
```

规则：

- progress/heartbeat/log 不进 journal；
- effect receipt 状态放在 receipt authority，journal 只保存关键 observation；
- setup/teardown 如果只是 Mote adapter 内部实现，不逐个产生公共事件；它们的结果进入 attempt terminal/reclamation receipt；
-未来新增事实通过 schema version 和 reducer 扩展，不为每个内部方法建立事件。

## 9. 最小 Provenance

Phase 1 必须记录：

- materialized dataset ID/digest/schema；
- case IDs/digests；
- fixture tree digest；
- Mote commit/dirty marker；
- frozen Task/Evaluator/Codec implementation manifest；
- model route request和实际 provider/model observations；
- prompt/toolset/skill/MCP manifest digest；
- permission policy digest；
- Python/OS/architecture；
- executor type/version/capabilities；
- random seed；
- timeout/concurrency；
- cache mode；
- artifact/redaction policy；
- audit/replay/rerun grade。

Phase 1 不记录：

-环境变量值或裸 secret hash；
-整个 pip environment dump；
-未脱敏 dirty diff；
-不稳定绝对路径；
-声称 exact 的外部服务状态。

缺少依赖 lock digest时标记 rerun grade partial，不阻止基础 eval 运行。

## 10. 最小 Metric/Report

MetricDefinition 保留：

```text
metric_id
kind
unit
better_direction
aggregation
```

首版聚合：

- assertion：passed/observed/all-runs；
- scalar：count/missing/min/max/mean；
- label：count distribution；
-所有 metric 同时显示 task failure 和 evaluator failure 数量。

不提供：

-自动总分；
-权重；
-显著性；
-置信区间；
-模型排名；
-跨不同 digest 的比较。

报告是 projection，失败不改变 Experiment execution terminal。

## 11. Python API 的最小面

首版公开 API 建议只包含：

```python
from mote.eval import (
    Case,
    Dataset,
    EvalRunConfig,
    ExperimentReport,
    run_dataset,
)
```

Mote adapter：

```python
from mote.eval.adapters.mote import (
    CodingTaskInput,
    MoteCodingTask,
)
```

不公开：

- reducer state internals；
- EffectCommand；
- Local executor classes；
- journal implementation；
- workspace paths；
- mutable projection models；
- migration internals。

`Dataset.evaluate()` 风格可以后续作为薄 convenience API；第一阶段保持唯一 application service，避免 Dataset 同时成为数据对象和执行门面。

## 12. CLI 的最小面

```bash
python -m mote.eval run <dataset>
python -m mote.eval show <experiment-id>
python -m mote.eval verify <experiment-id>
```

明确不支持：

```text
resume running attempt
repair journal
compare experiments
remote executor
dynamic evaluator plugin
automatic retry
```

CLI 遇到不支持的操作应明确报 capability error，不保留无行为的占位参数。

## 13. 分阶段演进

### Phase 0：边界准备

-修订 canonical 主设计；
-复用 EventEnvelope/EventJournal/ArtifactRef/ContentDigest；
- Product headless facade；
- Artifact scoped host ports；
- architecture tests。

### Phase 1：单机最小闭环

-本评审定义的全部最小能力；
-一个真实 Mote Coding case；
-多个只读 deterministic evaluators；
-正常、失败、取消、超时、coordinator crash 的测试。

### Phase 2：可恢复本地执行

- durable process supervisor；
- restart attach/reconcile；
-显式 resume；
- evaluator overlay + PolicyBoundCommandService；
- task/evaluator retry；
- side-effect profiles；
- budget reservation/settlement。

### Phase 3：实验比较

- repeat；
- paired/unpaired comparison；
- versioned report policies；
-置信区间；
- cache snapshots；
- experiment fork。

### Phase 4：扩展执行

- sandbox/untrusted evaluator；
- browser/visual evaluator；
- remote executor；
- distributed coordinator lease；
- dataset evaluator。

每一阶段只扩展既有 authority/receipt/capability 接缝，不新增第二套 runner。

## 14. Phase 1 风险接受清单

以下限制必须进入 CLI、report 和 provenance，不能只写在设计文档：

```text
single_host_only = true
single_coordinator_only = true
active_attempt_resume = false
automatic_retry = false
evaluator_retry = false
command_evaluator = false
untrusted_plugins = false
external_side_effect_isolation = partial/none
distributed_execution = false
```

如果 task 配置要求不支持的能力，实验在 `ExperimentDeclared` 前失败，不能运行后降级。

## 15. Phase 1 验收标准

1. 现有 EventJournal replay 与 live reducer 逐字段一致。
2. 第二个 runner 无法并发修改同一 experiment。
3. 每个 attempt 使用 fresh workspace 和新 session。
4. 正常、失败、取消、超时均产生 terminal/reclamation receipts。
5. coordinator 在 attempt 活动期崩溃后，实验进入 in_doubt，不自动重跑。
6. 已完成 case 在重启后不会重复执行。
7. evaluator 只读取 immutable snapshot，不能修改被测结果。
8. ArtifactRef 使用现有 Contracts 类型，journal 不引用未提交 artifact。
9. 所有 durable values 经过 codec，digest 有 golden vectors。
10. metric schema、identity 和 result 稳定序列化。
11. show/report 只依赖 journal/reducer，不加载 Task/Evaluator executable。
12. ask 在 headless 下 fail-closed。
13. Engine/Application/Agent/Terminal/Python/子进程清理有证据；无法验证时不记 succeeded。
14. architecture tests 阻止 eval import runtime/product 私有路径。
15. capability 不支持时 fail-fast，不静默降级。

## 16. 删除建议

从 Phase 1 章节删除或后移：

- LocalProcessCaseExecutor 的“崩溃后可恢复”暗示；
- max_attempts > 1；
- evaluator retry；
- CommandEvaluator；
- compare；
- repair；
- remote/sandbox executor；
-动态 plugin registry；
- dataset evaluator；
- LLM Judge/浏览器；
- advanced report policy；
- durability mode；
- heartbeat lease store；
-通用 effect registry；
- migration runner。

保留相应稳定 identity、port 或 schema seam，但不提供未兑现的配置项和空实现。

## 17. 最终判断

十年零负债不等于第一年实现十年的全部功能。真正的零负债标准是：

-现在实现的每个保证都能被测试证明；
-暂时做不到的能力有明确 capability=false；
-恢复不了的状态进入 in_doubt；
-不支持的配置在执行前拒绝；
-未来能力沿稳定 authority/receipt/codec 接缝扩展。

建议立即以本评审为范围基线，合并重写 canonical 主设计。只有 Phase 0 边界准备和 Phase 1 最小闭环进入首批实施计划，其余能力留在路线图，不进入首版类图和 CLI surface。
