# Mote Eval 长期架构设计

> 状态：实现前架构规格。
>
> 设计目标：在 Mote 包内新增独立顶层 `eval/` 包，形成可离线运行、可恢复、可复现、可比较、可扩展十年以上的评测基座，不引入跨层旁路或临时兼容债务。

## 1. 设计原则

1. **事实先于投影**：append-only journal 是实验编排的唯一真相源；状态、报告和 CLI 展示均由同一个纯 reducer 派生。
2. **身份先于名称**：dataset、case、task、evaluator、metric、artifact 均使用稳定 ID、版本和内容摘要；名称只用于显示。
3. **可复现先于便捷**：每次实验固化完整 provenance；无法证明可比较的实验不得静默比较。
4. **隔离先于重试**：每个 attempt 默认从相同 fixture 快照创建全新 workspace、session 和执行所有权域。
5. **端口先于实现**：CaseExecutor、journal、artifact、workspace、codec 和 Product facade 从第一版即有稳定端口；本地实现不是协议本身。
6. **能力先于裸对象**：Task/Evaluator 获得窄能力，不直接获得可任意读写的内部对象或 Mote runtime。
7. **终态只有一次**：case 只有在 teardown 成功或失败被记录后才提交终态；中间结果只是 checkpoint。
8. **语义显式**：不隐式归一化指标、不猜测同步/异步、不把 ask 自动批准、不把 retry 当续跑。
9. **所有权闭合**：Agent、子 Agent、Terminal、Python、后台任务和子进程必须属于可取消、可回收的 execution scope。
10. **零反向依赖**：现有五层不得 import `eval/`；Eval 对 Mote 的认识只存在于 Mote adapter。

## 2. 定位与边界

`mote.eval` 是一个最外层评测应用，不是 Mote 既有五层中的新基础层。

```text
contracts <- kernel <- runtime <- orchestration <- product
                                                   ^
                                                   |
                                            eval/adapters/mote
                                                   ^
                                                   |
eval/domain <- eval/application <- eval/infrastructure/reporting/cli
```

严格依赖规则：

- `eval/domain` 只依赖 Python 标准库及 domain 自身模块。
- `eval/application` 只依赖 `eval/domain` 和 application ports。
- `eval/infrastructure` 实现 application ports，只依赖 `eval/domain`、`eval/application` 和必要的第三方基础库。
- `eval/reporting` 只消费 domain 投影和 application 查询端口。
- `eval/adapters/mote` 只依赖 `eval/domain`、`eval/application`、`contracts` 与 `product` 的公开 API。
- `eval/cli` 只负责 composition、参数解析和展示，可依赖 application、infrastructure、reporting 与已注册 adapters。
- `eval/adapters/mote` 禁止直接组装或 import `runtime`、`orchestration` 内部实现。
- `eval/` 其他目录禁止 import `product`、`runtime` 或 `orchestration`。
- 经 ADR 批准的稳定端口是唯一例外；例外必须进入 architecture test 白名单，不能使用局部 import 绕过。
- `contracts`、`kernel`、`runtime`、`orchestration`、`product` 均不得 import `eval`。
- Eval 配置和状态不得进入 `RoleSchema` 或 `RoleState`。

Mote 应在 Product 层提供稳定的非交互式 Application facade。CLI、Eval 和未来服务端都消费同一个 facade；Eval 不从 CLI 私有函数复制装配路径。

## 3. 信任与隔离模型

### 3.1 输入信任

- dataset/YAML/JSON 是不可信声明式输入。
- 声明式配置只能引用注册表中的 task、evaluator、codec 和 report policy。
- 禁止任意 Python import path、表达式、shell 字符串和动态代码执行。
- secret 不得出现在 dataset；由 Mote 配置系统解析，并只记录来源标识与脱敏 fingerprint。

### 3.2 实现信任

- 内置或应用启动时显式注册的 Python Task/Evaluator 是 trusted code。
- trusted in-process evaluator 只有 API 约束，不构成安全沙箱。
- 不可信自定义 Python evaluator 必须由支持 OS 沙箱的 CaseExecutor/EvaluatorExecutor 执行；第一阶段不支持加载这类代码。
- 文档和 API 不宣称 Python 进程内可以阻止恶意实现读写任意文件或修改全局状态。

### 3.3 执行级别

```text
InProcessCaseExecutor
  仅用于受信任纯函数、框架单元测试和显式选择的低风险任务；
  不承诺强超时、进程回收或文件系统隔离。

LocalProcessCaseExecutor
  Coding Agent 的默认执行器；每个 attempt 独立进程、workspace、session 和进程组；
  支持强制终止与资源回收验证。

SandboxedCaseExecutor（后续）
  面向不可信 task/evaluator，提供 OS/container 沙箱和最小文件系统能力。

RemoteCaseExecutor（后续）
  面向分布式执行，遵循同一 execution request/result 和 lease 协议。
```

## 4. 目标包结构

```text
eval/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── ids.py                 # 稳定 ID value objects
│   ├── values.py              # JsonValue、Digest、Version
│   ├── dataset.py             # Dataset、Case 声明
│   ├── metrics.py             # MetricDefinition/Observation
│   ├── artifacts.py           # ArtifactRef/ArtifactManifest
│   ├── provenance.py          # ExperimentProvenance
│   ├── results.py             # attempt/evaluation/case/experiment 结果
│   ├── events.py              # versioned event envelope + payload union
│   └── state.py               # reducer、状态机与合法 transition
├── application/
│   ├── __init__.py
│   ├── ports.py               # 所有外部能力端口
│   ├── requests.py            # 可序列化 execution request/result
│   ├── runner.py              # ExperimentRunner 门面
│   ├── case_machine.py        # 单 case 状态机驱动
│   ├── retry.py               # retry policy 与 attempt 选择
│   ├── recovery.py            # resume/restart/reconcile
│   └── queries.py             # show/report 查询模型
├── infrastructure/
│   ├── __init__.py
│   ├── journal.py             # durable JSONL committer/replay
│   ├── artifacts.py           # content-addressed artifact store
│   ├── workspace.py           # fixture snapshot/workspace lease
│   ├── codecs.py              # JSON-native 与注册 codec
│   ├── local_executor.py      # LocalProcessCaseExecutor
│   ├── inprocess_executor.py  # 受限用途 executor
│   ├── registry.py            # 声明式类型注册表
│   └── projection_store.py    # 原子写入可重建投影
├── adapters/
│   ├── __init__.py
│   └── mote/
│       ├── __init__.py
│       ├── application.py     # Product headless facade adapter
│       ├── coding_task.py
│       ├── lifecycle.py
│       ├── command_service.py # Product 公开的受策略约束命令服务适配
│       └── evaluators.py
├── reporting/
│   ├── __init__.py
│   ├── aggregate.py
│   ├── compare.py
│   ├── json_report.py
│   └── terminal.py
└── cli/
    ├── __init__.py
    └── __main__.py
```

公开 API 仅从 `eval/__init__.py` 和 `eval/adapters/mote/__init__.py` 导出。包内模块默认不稳定，除非进入显式 public API 清单。

## 5. 稳定身份与摘要

名称不参与长期身份。核心 identity：

```text
ExperimentId   每次实验唯一
DatasetId      dataset 作者声明的稳定逻辑身份
CaseId         dataset 内稳定逻辑身份，重命名 display_name 不改变它
RunId          一个 case 的一次 repeat
AttemptId      一个 run 的一次 task attempt
EvaluatorId    evaluator 实现的稳定身份
MetricId       evaluator namespace 内稳定身份
ArtifactId     内容寻址或稳定生成 ID
EventId        journal 全局幂等 ID
```

所有配置摘要使用规范化 JSON 后的强摘要算法。算法名称是摘要值的一部分，例如 `sha256:<hex>`，便于未来迁移。

比较与恢复至少使用：

```text
dataset_id + dataset_digest + dataset_schema_version
case_id + case_digest
task_id + task_version + task_config_digest
evaluator_id + evaluator_version + evaluator_config_digest
metric_id + metric schema
fixture_digest
```

持久化的 task、evaluator、codec 和 report policy 版本不得为空。开发者未声明版本时，注册失败，而不是写入 `None`。

## 6. Dataset 与 Case

```python
@dataclass(frozen=True, slots=True)
class Case(Generic[InputsT, ExpectedT]):
    case_id: CaseId
    display_name: str
    inputs: InputsT
    expected_output: ExpectedT | None
    metadata: Mapping[str, JsonValue]
    evaluators: tuple[EvaluatorSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class Dataset(Generic[InputsT, ExpectedT]):
    dataset_id: DatasetId
    display_name: str
    schema_version: str
    cases: tuple[Case[InputsT, ExpectedT], ...]
    default_evaluators: tuple[EvaluatorSpec, ...] = ()
```

第一阶段只有两类 case evaluator：

- `default_evaluators`：应用于每个 case；
- `Case.evaluators`：只应用于指定 case。

两者使用同一个 `Evaluator` 协议，执行顺序为 default 后 case-specific。第一阶段不提供 dataset evaluator，避免一个协议承载两个生命周期。

真正的 dataset 级评测后续使用独立协议：

```python
class DatasetEvaluator(Protocol):
    async def evaluate_dataset(
        self,
        context: DatasetEvaluationContext,
    ) -> Sequence[MetricObservation]: ...
```

它只能在所有 case/repeat 已进入终态后执行，输入是按 `(case order, repeat_index)` 稳定排序且已 codec 化的 `CaseResultView`。该协议加入前需要独立 ADR。

校验规则：

- `dataset_id`、`case_id` 在各自作用域唯一且稳定。
- display name 非空但可修改，不参与 identity。
- inputs、expected output 和 metadata 必须由声明的 codec 编码。
- dataset digest 覆盖 schema version、case 顺序、case digest 和 evaluator specs。
- case digest 覆盖 inputs、expected output、metadata 和 evaluator specs，不覆盖 display name。

## 7. Codec 契约

任意 Python 泛型值不能直接进入 durable boundary。所有跨进程、journal、result 和 report 的值必须经过显式 codec：

```python
class ValueCodec(Protocol[T]):
    codec_id: str
    version: str

    def encode(self, value: T) -> JsonValue: ...
    def decode(self, value: JsonValue) -> T: ...
```

规则：

- 默认 `JsonValueCodec` 只接受 JSON-native 值。
- dataclass/Pydantic 类型需要显式注册 codec，包含稳定 codec ID 和非空版本。
- 二进制、大文本、transcript、trace 和目录树进入 ArtifactStore，JSON 只保存 `ArtifactRef`。
- codec 配置和版本进入 provenance 与 digest。
- decode 失败是稳定的 boundary error，不能用原始 Python pickle 兜底。
- 禁止 pickle 作为持久化或远程执行协议。

## 8. Task、Evaluator 与窄能力

### 8.1 EvalTask

核心协议始终异步：

```python
class EvalTask(Protocol[InputsT, OutputT]):
    task_id: str
    version: str

    async def run(self, inputs: InputsT, context: TaskContext) -> OutputT: ...
```

同步函数不由 runner 猜测，必须显式包装：

- `FunctionTask(sync_callable, worker=...)`
- `AsyncFunctionTask(async_callable)`

同步函数在有界 worker 中执行。线程执行器无法强杀超时 Python 函数，因此 `FunctionTask` 只有 best-effort timeout；要求强超时的同步任务必须选择 process executor。

### 8.2 TaskContext

```python
@dataclass(frozen=True, slots=True)
class TaskContext:
    experiment_id: ExperimentId
    case_id: CaseId
    run_id: RunId
    attempt_id: AttemptId
    repeat_index: int
    attempt_index: int
    workspace: WorkspaceLease
    artifacts: ArtifactWriter
    cancellation: CancellationToken
    seed: int
```

`WorkspaceLease` 只暴露当前 attempt 的 scope 与受控路径解析，不暴露 workspace 管理器。

### 8.3 Evaluator

```python
class Evaluator(Protocol[InputsT, OutputT, ExpectedT]):
    evaluator_id: EvaluatorId
    version: str

    async def evaluate(
        self,
        context: EvaluatorContext[InputsT, OutputT, ExpectedT],
    ) -> Sequence[MetricObservation]: ...
```

`EvaluatorContext` 不提供裸写权限：

```python
@dataclass(frozen=True, slots=True)
class EvaluatorContext(Generic[InputsT, OutputT, ExpectedT]):
    inputs: InputsT
    output: OutputT
    expected_output: ExpectedT | None
    metadata: Mapping[str, JsonValue]
    attempt: AttemptResultView
    workspace: WorkspaceView
    artifacts: ArtifactWriter
    commands: CommandEvaluationService | None
```

- `WorkspaceView` 是只读能力 API，提供受控相对路径读取、枚举和摘要。
- `ArtifactWriter` 只能写 evaluator 自己的 artifact namespace。
- `CommandEvaluationService` 是受策略约束的显式能力，不是 shell/terminal 对象。
- 这些 API 对 trusted evaluator 是约束和审计接缝，不是恶意代码安全边界。

### 8.4 CaseLifecycle

Lifecycle 是 attempt-scoped 的资源协议，不是可随意继承并保存状态的基类：

```python
class CaseLifecycle(Protocol[InputsT, OutputT]):
    lifecycle_id: str
    version: str

    async def setup(self, context: SetupContext[InputsT]) -> SetupReceipt: ...

    async def prepare_evaluation(
        self,
        context: PrepareEvaluationContext[InputsT, OutputT],
    ) -> PreparationReceipt: ...

    async def teardown(self, context: TeardownContext) -> CleanupReceipt: ...
```

约束：

- 每个 hook 都接收稳定 operation ID；外部副作用必须以该 ID 实现幂等或对账。
- receipt 必须可 codec 化，记录已获得资源及其所有权；不能只返回 `None`。
- setup 未完成也必须根据已 durable 的部分 receipt 尝试 teardown。
- prepare-evaluation 只能创建 evaluator 输入投影和 artifact，不能修改 task output 事实。
- teardown 接收已知资源 manifest，并逐项返回 released、missing、failed 或 unknown。
- hook started 后进程丢失时，恢复器先调用 executor reconcile；只有协议声明可重入时才重放 hook。

## 9. 指标契约

Evaluator 不返回松散 union，统一返回 `MetricObservation`：

```python
@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: MetricId
    display_name: str
    kind: Literal["assertion", "scalar", "label"]
    unit: str | None
    better_direction: Literal["higher", "lower", "none"]
    aggregation: Literal["mean", "sum", "min", "max", "rate", "distribution", "none"]


@dataclass(frozen=True, slots=True)
class MetricObservation:
    definition: MetricDefinition
    value: bool | int | float | str
    reason: str | None = None
    sample_weight: float | None = None
```

约束：

- assertion 只接受 `bool`；解析时必须先判断 bool，不能利用 bool 是 int 子类的行为。
- scalar 只接受有限 `int/float`，拒绝 NaN 和无穷值。
- label 只接受字符串。
- evaluator 在注册时声明其 metric definitions；运行时不得产生未声明 metric。
- mapping 型输出由 evaluator adapter 展开成多个 observation，mapping key 必须映射到稳定 MetricId。
- report 不猜测单位、方向或聚合方法。
- 第一阶段不默认生成总分；总分只能由显式、版本化 `ReportPolicy` 产生。

稳定比较键：

```text
dataset_id + dataset_digest
case_id + case_digest
evaluator_id + evaluator_version + evaluator_config_digest
metric_id + metric definition digest
```

## 10. Artifact 契约

```python
@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: ArtifactId
    digest: Digest
    media_type: str
    byte_size: int
    relative_location: str
    producer_phase: str
    producer_id: str
    retention: Literal["ephemeral", "run", "experiment", "pinned"]
    sensitivity: Literal["public", "internal", "secret"]
    redaction_status: Literal["not_required", "redacted", "pending"]
    integrity_status: Literal["available", "missing", "corrupt"]
```

规则：

- ArtifactStore 控制路径和写入，Task/Evaluator 不自创可持久化绝对路径。
- artifact 写入采用临时文件、内容摘要校验和原子发布。
- 所有 durable result 只保存 ArtifactRef。
- report 默认不公开 `secret` artifact。
- replay 时缺失或损坏 artifact 不修改历史事实，而在完整性投影中标记状态。
- 大输出、traceback、stdout/stderr、transcript 和 workspace archive 均通过 artifact 契约管理。

## 11. CaseExecutor 端口

CaseExecutor 从第一版存在：

```python
class CaseExecutor(Protocol):
    async def execute(self, request: CaseExecutionRequest) -> CaseExecutionReceipt: ...
    async def cancel(self, lease: ExecutionLease) -> CancellationReceipt: ...
    async def reconcile(self, lease: ExecutionLease) -> ExecutionStatus: ...
```

`CaseExecutionRequest` 必须完全可 codec 化，至少包含：

- experiment/case/run/attempt identity；
- task/evaluator registry refs 与版本；
- codec refs；
- fixture digest 和 workspace policy；
- timeout、seed、resource limits；
- artifact/journal scoped capabilities；
- provenance snapshot reference；
- headless permission policy。

`CaseExecutionReceipt` 包含 execution lease、worker identity、状态、heartbeat 和 artifact/result refs。调用方不能通过进程内对象引用获取结果。

所有权规则：

- executor 接受 request 后返回 lease，lease 是 cancel/reconcile 的唯一 authority。
- worker 必须定期提交 heartbeat；丢失 worker 进入 `executor_lost`，不是普通 task failure。
- timeout 由拥有 lease 的 executor 判定并执行终止。
- runner 崩溃恢复后先 reconcile lease，再决定 resume、retry 或标记 lost；禁止盲目重复执行。
- 远程和本地 executor 共享相同 attempt identity、幂等键与 receipt 语义。

## 12. Workspace 与 Retry

### 12.1 Workspace 身份

```text
FixtureSnapshot  只读、内容寻址、由 fixture_digest 标识
Run              一个 case repeat，逻辑上包含多个 attempt
AttemptWorkspace 每个 attempt 从同一 FixtureSnapshot 物化
```

默认策略：

```text
RetryWorkspacePolicy.FRESH
  每个 attempt 创建新 workspace；
  新 session、新 Agent execution scope、新进程组；
  seed 是否相同由实验 retry policy 明确声明。
```

可选策略：

```text
RetryWorkspacePolicy.REUSE
  显式续跑语义；
  结果标记 isolated_retry=false；
  provenance 记录前置 attempt 和复用原因；
  默认报告不与 FRESH retry 结果直接比较。
```

所有 attempt 都保留。`RunResult` 不覆盖失败 attempt，而是通过 `selected_attempt_id` 指向 retry policy 选中的 terminal attempt。

Fixture snapshot：

- 创建时验证路径穿越和符号链接逃逸；
- 计算规范化 tree digest；
- attempt 只从同一 digest 物化；
- mutable build cache 若启用，必须作为显式、带摘要的外部输入进入 provenance，默认关闭共享写缓存。

## 13. 正式 Case 状态机

### 13.1 正交状态

不要用单个 status 压缩全部事实：

```text
execution_status:
  pending | running | succeeded | failed | timed_out | cancelled | executor_lost

evaluation_status:
  pending | running | succeeded | partial | failed | skipped | cancelled

cleanup_status:
  pending | running | succeeded | failed

completion_status:
  open | completed | aborted
```

### 13.2 Phase

```text
declared
→ workspace_provisioning
→ setup
→ task_attempts
→ prepare_evaluation
→ evaluators
→ teardown
→ finalizing
→ completed
```

任一阶段可进入 cancellation/timeout/failure 路径，但都必须尽可能进入 teardown。只有 teardown 已记录成功或失败后才允许 `CaseCompleted`。

### 13.3 正常流程

```text
CaseDeclared
→ WorkspaceProvisionStarted/Completed
→ SetupStarted/Completed
→ TaskAttemptStarted
→ TaskAttemptCompleted | Failed | TimedOut | Cancelled | ExecutorLost
→ RetryScheduled → next fresh attempt（可重复）
→ PrepareEvaluationStarted/Completed
→ EvaluatorStarted
→ EvaluatorCompleted | Failed | TimedOut | Cancelled（逐个）
→ TeardownStarted
→ TeardownCompleted | Failed
→ CaseResultFinalized
→ CaseCompleted
```

Task 没有成功 output 时，每个本应运行的 evaluator 必须写 `EvaluatorSkipped(reason=task_not_succeeded)`，从事实层区分规则跳过和进程崩溃。

### 13.4 事件闭包

状态机必须能表达每个 phase 的开始、成功、失败、超时、取消、跳过和所有权丢失。第一版 payload union 至少包含：

```text
ExperimentDeclared
ExperimentStarted
ProvenanceCommitted

CaseDeclared
WorkspaceProvisionStarted
WorkspaceProvisionCompleted
WorkspaceProvisionFailed
SetupStarted
SetupCheckpointed
SetupCompleted
SetupFailed

TaskAttemptScheduled
TaskAttemptStarted
TaskAttemptHeartbeat
TaskAttemptCompleted
TaskAttemptFailed
TaskAttemptTimedOut
TaskAttemptCancelled
TaskAttemptExecutorLost
RetryScheduled
RetryExhausted

PrepareEvaluationStarted
PrepareEvaluationCompleted
PrepareEvaluationFailed
PrepareEvaluationTimedOut
PrepareEvaluationCancelled

EvaluatorScheduled
EvaluatorStarted
EvaluatorCompleted
EvaluatorFailed
EvaluatorTimedOut
EvaluatorCancelled
EvaluatorSkipped

TeardownStarted
TeardownCheckpointed
TeardownCompleted
TeardownFailed

CaseResultFinalized
CaseCompleted
CaseAborted

ReportProjectionStarted
ReportProjectionCompleted
ReportProjectionFailed
ExperimentCompleted
ExperimentFailed
ExperimentCancelled
```

不能仅根据缺少事件推断 skipped、timed out 或 cancelled。新增 phase 时必须同时提交状态 transition、事件 payload、reducer、recovery decision 和 crash-boundary test。

### 13.5 终态语义

- `CaseCompleted` 是唯一正常终态提交，必须引用 finalized CaseResult digest。
- `CaseAborted` 用于无法完成 teardown/finalization 的管理员或恢复决策，不等价于 task failed。
- cleanup failure 不覆盖 execution/evaluation 状态；`cleanup_status=failed` 独立呈现。
- 默认实验成功策略可要求 cleanup 全部成功，但这是版本化 ExperimentPolicy，不改变 case 原始事实。
- 中间 Task/Evaluator 结果可 checkpoint，但不能使 case 提前显示 completed。

合法 transition 在 `domain/state.py` 集中定义。实时执行和 replay 使用同一个纯 reducer；非法 transition 产生 reducer error，不做静默修复。

## 14. Journal、Envelope 与 Reducer

### 14.1 Event envelope

```python
@dataclass(frozen=True, slots=True)
class EvalEventEnvelope:
    schema_version: int
    sequence: int
    event_id: EventId
    experiment_id: ExperimentId
    case_id: CaseId | None
    run_id: RunId | None
    attempt_id: AttemptId | None
    occurred_at: str
    event_type: str
    payload: JsonValue
```

约束：

- 单 experiment journal 单写者。
- 单写者是 experiment coordinator/committer，不是任一 case worker。并发 worker 只能通过 `JournalFactSink` 提交带幂等键的候选事实；committer 分配全局 sequence、durable commit 后再返回 acknowledgment。
- sequence 从 1 单调递增且无重复；event_id 用于幂等去重。
- committer 同时校验 expected previous sequence，防止并发写者。
- worker 在未收到 durable acknowledgment 前不得把 phase 视为已提交；重连后可用同一 event ID 重交事实。
- worker 本地 receipt/artifact 发布与 journal fact 之间通过 operation ID 对账；不声称跨文件系统原子事务。
- payload 由 event schema version 校验。
- reducer 是纯函数：`reduce(state, envelope) -> state`。
- runner 的 live projection、resume、CLI show 和 report 全部调用同一个 reducer。

### 14.2 Durability policy

提供显式策略：

```text
strict    每个终态/所有权事件写入后 flush + fsync；目录元数据在新文件/原子替换时 fsync。
balanced  phase boundary 与终态 fsync，普通进度事件批量提交。
relaxed   仅 flush，用于测试和可丢弃实验，不得标记 crash-safe。
```

默认 `balanced`；case terminal、execution lease、retry decision、experiment terminal 事件必须 durable commit 后才能对外确认。

### 14.3 Replay 行为

- 最后一个无换行或 JSON/schema 不完整的尾部记录视为 torn tail，忽略并记录 recovery diagnostic。
- journal 中间出现损坏记录是 integrity failure，停止 replay，禁止越过损坏位置猜测状态。
- 相同 event_id 且内容摘要相同的重复事件幂等忽略；相同 ID 内容不同是 integrity failure。
- sequence gap、倒退或冲突是 integrity failure。
- 未知 event type：若 envelope schema 兼容则保留 opaque event，并按事件声明的 compatibility 策略决定能否继续；未知状态转换事件默认停止投影，不能静默忽略。
- 新 schema 通过显式 migrator 链迁移到当前 reducer 输入；原 journal 永不原地改写。
- migration 输出新 journal/projection，并记录 source digest、migrator IDs 和版本。

### 14.4 Projection

- report/state projection 是可删除、可重建的派生物。
- 写入临时文件，fsync 后原子 replace，再按策略 fsync 目录。
- projection 保存 source journal digest、last sequence 和 reducer version。
- projection 与 journal 不匹配时丢弃 projection 并 replay，禁止混合使用。

## 15. Recovery、Resume 与 Restart

```text
resume
  恢复同一个 ExperimentId；replay journal；reconcile 所有非终态 lease；
  只执行状态机允许的后续动作。

restart
  创建新 ExperimentId；复用 dataset/config/provenance 输入；
  不继承旧 experiment 的 case 状态。

retry
  同一个 RunId 下创建新 AttemptId；必须由 RetryScheduled durable event 授权。
```

恢复规则：

- 已 durable 完成的 phase 不重复执行。
- started 但无 terminal event 的外部执行先 reconcile，不能直接重跑。
- setup/prepare/teardown 必须声明幂等键或由 executor 保证 attempt scope 一次性；恢复器不假设任意 hook 可安全重复。
- evaluator completed 后崩溃不得重新执行该 evaluator，除非结果 artifact 损坏且恢复策略显式授权 repair。
- CaseResultFinalized 后但 CaseCompleted 前崩溃，可验证 digest 后幂等补交 CaseCompleted。
- 每个 phase boundary 都必须有进程级崩溃恢复测试。

## 16. CommandEvaluationService

Eval 不直接构造内部 command runner，也不能把 classifier 当完整授权。

Product 应提供公开的受策略约束命令执行服务，Mote adapter 只消费该门面。Headless Eval policy：

- deny 永远拒绝；
- ask 默认转换为 deny，并产生明确 observation/artifact；
- 禁止自动批准和权限升级；
- argv 序列执行，不经过 shell；
- cwd 固定在 attempt workspace；
- readable/writable roots 固定在 case scope；
- 使用完整 permission policy/pipeline 和 sandbox，而非仅调用 classifier；
- 超时或取消清理整个进程组；
- stdout/stderr 超限进入 ArtifactStore；
- 记录 policy version、decision trace digest 和 execution receipt。

如果 Product 公开门面无法提供上述保证，`CommandEvaluator` 不得进入首版，而不是在 Eval 中重建旁路。

## 17. Mote Coding Agent Adapter

### 17.1 Product facade

Product 提供稳定、非交互式 application facade，负责：

```text
创建 Product Application
→ 构造 root Coding Agent
→ 建立 Agent ownership/control scope
→ 执行单轮或多轮 prompt
→ 返回 typed outcome 与公开 artifacts
→ cancel/close application scope
→ 验证资源释放
```

该 facade 应复用现有 Application、CodingAgentFactory 和 Engine 所有权语义。CLI 与 Eval 都是调用方；不得创建 eval-only Role 装配。

### 17.2 Coding 输入输出

```python
@dataclass(frozen=True, slots=True)
class CodingTaskInput:
    prompt: str
    turns: tuple[str, ...] = ()
    fixture: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class CodingTaskOutput:
    final_output: str | None
    session_id: str
    outcome: JsonValue
    changed_files: tuple[str, ...]
    transcript: ArtifactRef | None
    token_usage: JsonValue
    cost: str | None
```

- 多轮在同一 attempt 内复用 Role、session 和 workspace。
- retry 创建新 attempt，因此默认创建新 Role、session、workspace 和进程。
- repeat 创建新 RunId，同样完全隔离。
- Agent session rollout 与 Eval journal 分开记账，通过 experiment/run/attempt/session IDs 关联。
- Eval 不读取 Role 私有状态来构造结果；所有结果来自 Product facade 的公开 receipt。

### 17.3 资源关闭验收

timeout/cancellation/normal completion 后必须验证：

- root Agent 和子 Agent 已停止；
- Engine/Application lifecycle 已关闭；
- Terminal/Python 持久会话已释放；
- background task/workflow 已取消或完成；
- preview/dev server 等子进程组已清理；
- journal 和 artifact writer 已提交或明确失败；
- 无跨 attempt singleton 泄漏。

无法验证时 cleanup status 不能记为 succeeded。

## 18. Provenance 与可复现性

`ExperimentProvenance` 是实验开始前生成并 durable commit 的不可变快照，至少包含：

```text
dataset_id / dataset_digest / dataset_schema_version
case digests
task_id / version / config_digest
evaluator IDs / versions / config digests / metric definitions
codec IDs / versions
report policy ID / version / config digest
Mote package version / git commit / dirty-tree digest or explicit dirty marker
Product application facade version
requested model route
actual provider/model/endpoint protocol（运行后追加 observation，不改原快照）
system prompt digest
toolset/skill/MCP manifest digests
permission and sandbox policy digests
Python version / implementation
OS / architecture
dependency lock digest
fixture tree digests
random seed strategy and concrete seeds
allowlisted environment names and redacted fingerprints
workspace isolation mode
CaseExecutor type/version
retry/timeout/concurrency policies
durability policy
artifact retention/redaction policy
```

规则：

- secret 只记录来源标识、secret version 和不可逆脱敏 fingerprint，不记录值。
- 动态事实（实际 provider/model、fallback、token/cost）通过 provenance observation event 追加。
- dirty worktree 必须记录可重建 diff artifact，或明确标记 experiment non-reproducible。
- provenance 缺失关键字段时实验仍可运行，但必须标记 reproducibility grade，不得声称完全可复现。

推荐等级：

```text
exact       所有输入、实现、fixture 和环境均有可解析版本/摘要
bounded     外部 provider 等不可冻结因素已标识，但本地输入完整
partial     存在未摘要实现、dirty state 或环境缺口
unknown     旧数据或关键 provenance 缺失
```

## 19. 报告与比较

`EvaluationReport` 只能由 reducer 产生的 `ExperimentState` 构建，不直接扫描 workspace 猜测事实。

报告包含：

- experiment/provenance 摘要与 reproducibility grade；
- 每个 case/run/attempt 的正交状态；
- selected attempt 与全部历史 attempts；
- metric observations；
- task/evaluator/cleanup/executor/integrity failure；
- duration、token、cost 和 artifact refs；
- repeat 聚合。

比较前执行 compatibility check：

- dataset/case digest；
- evaluator identity/config/version；
- metric definition digest；
- retry workspace policy；
- isolation mode；
- report policy；
- provenance 中影响语义的差异。

结果分为：

```text
comparable       可直接计算差异
conditionally_comparable  存在已知差异，必须展示原因
not_comparable   身份或指标语义不兼容，拒绝生成数值差异
```

名称变化不破坏比较；digest 或 metric schema 变化不能被同名掩盖。

## 20. 首批 Evaluator

```text
TextMatchEvaluator
FileExistsEvaluator
FileContentEvaluator
JsonEvaluator
ChangedFilesEvaluator
CommandEvaluator（仅在 Product policy service 就绪后启用）
```

每个 evaluator 必须声明：

- evaluator ID 和非空版本；
- 配置 codec 与 config digest；
- 完整 metric definitions；
- 所需能力集合；
- timeout/cancellation 支持；
- artifact 类型与 sensitivity；
- 是否允许 in-process。

## 21. CLI 与声明式配置

```bash
python -m mote.eval run evals/coding.yaml
python -m mote.eval resume <experiment-id>
python -m mote.eval show <experiment-id>
python -m mote.eval compare <experiment-a> <experiment-b>
python -m mote.eval verify <experiment-id>
```

CLI 只做 composition、参数解析和展示，所有执行/恢复/比较语义来自 application services。

示例：

```yaml
schema_version: "1"
dataset_id: coding-smoke
display_name: Coding Smoke
task:
  type: mote-coding
  version: "1"
  config:
    model_route: default
executor:
  type: local-process
  version: "1"
retry:
  max_attempts: 2
  workspace_policy: fresh
cases:
  - case_id: todo-app
    display_name: Todo App
    inputs:
      prompt: Build a todo application.
      fixture: fixtures/web-empty
    evaluators:
      - type: file-exists
        version: "1"
        config:
          path: package.json
```

注册表按 `(kind, stable_id, version)` 解析实现。配置中声明的版本与安装版本不匹配时拒绝运行，不能自动选择 latest。

## 22. 错误模型

稳定错误分类至少包括：

```text
DatasetValidationError
CodecError
RegistryResolutionError
JournalIntegrityError
JournalCommitError
ProjectionError
WorkspaceProvisionError
SetupError
TaskExecutionError
TaskTimeoutError
TaskCancelledError
ExecutorLostError
PrepareEvaluationError
EvaluatorExecutionError
EvaluatorTimeoutError
EvaluatorCancelledError
CleanupError
ArtifactIntegrityError
RecoveryConflictError
ExperimentCancelledError
```

错误事实保存：稳定 type、phase、message、attempt、retryability、traceback ArtifactRef 和 cause classification。错误字符串不参与状态推导。

## 23. 第一阶段交付范围

第一阶段必须交付协议闭环，而不是先固化简化协议：

- domain identities、digests、codec、metric、artifact、provenance；
- versioned envelope、状态机、纯 reducer；
- durable local journal 与原子 projection；
- CaseExecutor port、InProcess 和 LocalProcess 两个实现；
- fresh-attempt workspace 与 fixture digest；
- ExperimentRunner、恢复与 reconcile 基础路径；
- trusted function task/evaluator；
- Mote Product headless facade；
- MoteCodingTask 与确定性 evaluator；
- JSON report 与 run/resume/show/verify CLI；
- architecture tests 和 phase-boundary crash tests。

第一阶段不包含：

- Web 服务/Web UI；
- 在线生产流量评测；
- dataset evaluator；
- LLM Judge；
- 浏览器视觉 evaluator；
- 不可信 Python 插件加载；
- container/remote/distributed executor；
- 第三方格式兼容层。

## 24. 测试与架构门禁

```text
ztest/eval/
├── domain/
│   ├── test_identity.py
│   ├── test_metrics.py
│   ├── test_events.py
│   └── test_state_machine.py
├── application/
│   ├── test_case_machine.py
│   ├── test_retry.py
│   ├── test_recovery.py
│   └── test_runner.py
├── infrastructure/
│   ├── test_journal.py
│   ├── test_artifacts.py
│   ├── test_codecs.py
│   ├── test_workspace.py
│   └── test_local_executor.py
├── adapters/mote/
│   ├── test_coding_task.py
│   ├── test_command_policy.py
│   └── test_resource_cleanup.py
├── reporting/
│   └── test_compare.py
└── test_architecture.py
```

架构测试必须保证：

1. `eval/domain` 不 import Mote 五层或第三方应用包。
2. `eval/application` 不 import infrastructure、product、runtime 或 orchestration。
3. 只有 `eval/adapters/mote` 可 import product/contracts 公开 API。
4. 整个 eval 不 import runtime/orchestration 私有模块。
5. 现有五层不 import eval。
6. 所有 production import 位于模块顶部。
7. 声明式注册表不支持任意 import path。

## 25. 实现前验收门槛

以下全部满足后，骨架才允许进入大规模实现：

1. live projection 与 journal replay 使用同一 reducer，结果逐字段一致。
2. journal 尾部半写、重复事件、sequence gap、冲突事件和未知新版事件都有确定行为。
3. durable policy 明确 flush/fsync/目录 fsync 和原子 projection 语义。
4. 每个 task attempt 默认从相同 fixture digest 创建干净 workspace、新 session 和新 execution scope。
5. 所有 attempt 被保留，terminal result 显式引用 selected attempt。
6. case 终态只在 teardown 完成或失败后提交。
7. execution/evaluation/cleanup/completion 状态保持正交。
8. 所有 durable value 均由声明 codec 编码，不存在 pickle fallback。
9. 每个 metric 有稳定 identity、类型、单位、方向和聚合语义。
10. 每个 artifact 有 digest、media type、producer、retention、sensitivity 和 integrity 状态。
11. 每次实验记录完整 provenance，并给出 reproducibility grade。
12. 比较器能拒绝语义不兼容的同名实验/指标。
13. headless ask 默认拒绝，不会等待不存在的用户或自动升级权限。
14. CommandEvaluator 只能使用 Product 公开的完整 policy/sandbox 服务。
15. timeout/cancellation 后验证 Agent、子 Agent、Terminal、Python、后台任务和子进程全部释放。
16. CaseExecutor 端口和可 codec 化 request/receipt 从第一版存在。
17. runner 恢复时先 reconcile execution lease，不会盲目重复执行。
18. 每个 phase boundary 均有真实进程崩溃恢复测试。
19. architecture test 阻止 eval 成为 runtime/product 装配旁路。
20. Eval journal 与 Agent session rollout 独立记账且具备稳定关联 ID。

## 26. 非目标与未来扩展规则

- 不为假设中的外部平台预留兼容字段。
- 不建立 generic utils/common 包。
- 不为了远程执行改变 domain identity、metric 或 artifact 语义；远程能力实现既有端口。
- 不为了 LLM Judge 放宽 evaluator 输出为任意 dict；它仍产生声明过的 MetricObservation。
- 不为了 Web UI 引入第二套状态存储；UI 读取 reducer projection。
- 不为了性能跳过 provenance、durable identity 或 journal ownership。
- 新增 dataset evaluator、sandbox executor、remote executor 或在线 eval 前必须分别通过 ADR，证明其复用现有状态机和 durable contracts。

这套边界的成功标准不是“能跑一次 benchmark”，而是十年后仍能解释：运行了什么、由谁执行、基于哪些输入、为何得到这些指标、能否恢复、能否复现，以及两个结果是否真的可比较。
