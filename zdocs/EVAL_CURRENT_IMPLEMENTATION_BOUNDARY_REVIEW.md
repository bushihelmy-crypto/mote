# Mote Eval 当前实现边界与设计收口意见

> 状态：实现前边界审核。
>
> 关联设计：[`EVAL_PACKAGE_SKELETON.md`](./EVAL_PACKAGE_SKELETON.md)

## 1. 结论

当前 Mote 已经能够可靠地构造、运行和关闭一个 Agent，但尚不具备组织、恢复、持久化和比较一个评测实验的能力。

```text
当前已有：
配置 → Product Application → Agent → RunOutcome/session/artifacts → cleanup

尚未实现：
Dataset → Experiment → Case → Run → Attempt
        → evaluator → metric → journal/reducer
        → recovery → report/comparison
```

因此，`EVAL_PACKAGE_SKELETON.md` 是目标架构规格，不是现状说明。当前仓库内不存在 `eval/` 生产包，不能把设计中的任何 Eval 类型、状态机或持久化保证视为已经实现。

实现起点也不应是直接创建 `DatasetRunner`。在 Eval 内核接入 Mote 之前，必须先关闭 Product headless facade、受策略约束命令服务、Artifact 单一契约和 Eval durable core 四个边界问题。

## 2. 当前已经实现的基础能力

### 2.1 Product Application 与 Engine 生命周期

当前已有：

- `product/composition/application.py::Application`；
- `runtime/engine.py::Engine`；
- Engine 对其创建的 Agent 具有明确所有权；
- `release()` 可以释放单个 Agent；
- `aclose()` 具有幂等、取消安全的关闭语义；
- Agent cleanup 失败会被保留并允许后续重试；
- Engine 在全部 Agent 成功释放后关闭共享 services。

这部分可以作为 Eval attempt 资源所有权的底座，但它目前只是内部应用生命周期能力，不等于完整的 Eval execution scope。

Eval 仍需验证并记录：

- root Agent 和子 Agent 是否全部停止；
- Terminal/Python 持久会话是否释放；
- background task/workflow 是否停止；
- Agent 启动的子进程组是否清理；
- cleanup 是否能形成可持久化 receipt。

### 2.2 Coding Agent composition root

`product/agents/factory.py::CodingAgentFactory` 已经负责构造具备完整 Product 能力的 Coding Agent，包括 toolsets、skills、LSP、background tasks 和 workspace 相关依赖。

它是正确的内部 composition root。Eval 不应复制 Role、RoleSchema、AgentWiring 或具体 runtime component 的装配逻辑。

### 2.3 Agent 执行

`runtime/agent/role.py::Role.run()` 已经提供类型化、持久提交的单次运行入口，并返回 `RunOutcome`。

它解决的是“一次 Agent turn 如何执行”，没有解决：

- case/run/attempt identity；
- fixture 和 workspace 隔离；
- retry；
- experiment journal；
- evaluator；
- metric；
- experiment recovery。

Eval 应把 `Role.run()` 视为被测 task 的内部执行能力，而不是 Eval runner 本身。

### 2.4 Session 事实与 replay

当前 session 已有独立的事件、rollout journal 和正向 replay 语义。它可以作为 Eval journal 设计的参考，也能作为 Coding attempt 的 transcript 事实来源。

但二者必须继续独立：

```text
Agent session rollout
  记录 Agent 历史、压缩与会话事实。

Eval experiment journal
  记录 experiment/case/run/attempt/evaluator/cleanup 编排事实。
```

两者只通过稳定的 experiment ID、run ID、attempt ID 和 session ID 关联。Eval 不得把实验事件塞入 Agent rollout，也不得用 Agent rollout 替代 experiment journal。

### 2.5 Artifact 契约

`contracts/artifact/` 已经存在成熟的 Artifact 契约，包括：

- `ArtifactRef`；
- content reference；
- retention；
- sensitivity；
- publication state；
- resolution policy；
- revision、publication 和 reconcile 类型。

这意味着 Eval 不应该再定义第二套通用 `ArtifactRef`。

### 2.6 权限系统

当前 runtime 已有：

- command classifier；
- permission rules；
- `PermissionEngine`；
- deny/ask/allow 优先级；
- deny/ask 的 bypass-immune 语义；
- 无交互 channel 时 ask fail-closed 的能力。

但这些仍是 runtime 内部执行管线。当前没有一个供 Eval 直接消费的 Product 公开命令执行门面。

## 3. 当前尚未实现的 Eval 能力

仓库内当前没有 `eval/` 目录，以下能力全部仅存在于设计文档：

- Dataset、Case 和声明式 loader；
- EvalTask、Evaluator、CaseLifecycle；
- stable dataset/case/run/attempt/evaluator/metric identity；
- ValueCodec；
- MetricDefinition 和 MetricObservation；
- Eval provenance；
- Eval artifact producer metadata；
- case 状态机；
- versioned Eval event envelope；
- durable journal committer；
- pure reducer；
- live projection 和 replay；
- CaseExecutor；
- execution lease、heartbeat、cancel 和 reconcile；
- fixture snapshot 和 tree digest；
- fresh-attempt workspace；
- retry 和 selected attempt；
- experiment resume/restart；
- report policy；
- comparison compatibility check；
- Eval CLI；
- MoteCodingTask adapter；
- Eval architecture tests；
- phase-boundary process crash tests。

实现计划和评审必须以“从零建设 Eval 子系统”为前提，不能把现有 session、CLI 或 Engine 能力误计为 Eval 已完成部分。

## 4. Product headless facade 的实际边界

### 4.1 当前 Application 仍是低级装配结果

现有 `product/composition/bootstrap.py::build_application()` 要求调用方提供：

```python
container
services
agent_factory
```

因此它不是面向 Eval、CLI 或服务端的高层 headless facade。调用方仍需理解 Product object graph 的构造方式。

### 4.2 CLI bootstrap 不能成为 Eval API

`product/entrypoints/cli/bootstrap.py::build_engine()` 已经能够完成完整装配，但它属于 CLI entrypoint，并带有 CLI 语义：

- CLI 配置加载；
- cwd 与本地配置发现；
- locale/scaffolding；
- CLI Role 构造策略；
- MCP/file-watch 等交互式便利能力；
- UI/consumer 的上下文假设。

Eval 直接 import 它会把 CLI 变成事实上的公共 composition API，并将交互式默认值带入评测环境。

### 4.3 应新增的 Product 能力

Product 应提供稳定、非交互式 application facade，例如：

```python
class HeadlessCodingApplication(Protocol):
    async def run(self, request: CodingRunRequest) -> CodingRunReceipt: ...
    async def cancel(self, execution_id: str) -> CancellationReceipt: ...
    async def reconcile(self, execution_id: str) -> CodingExecutionStatus: ...
    async def aclose(self) -> CleanupReceipt: ...
```

该 facade 应负责：

- 加载正式 Product 配置；
- 构造 ProductContainer、EngineServices 和 Application；
- 使用 CodingAgentFactory 构造 root Agent；
- 建立 AgentControl/ownership scope；
- 执行单轮或多轮 prompt；
- 返回公开、可序列化的 receipt；
- 取消和关闭全部资源；
- 输出资源释放结果。

CLI 与 Eval 应共同消费该 facade。不得创建只供 Eval 使用的第二套 Role 装配路径。

## 5. CommandEvaluator 的当前边界

### 5.1 当前不能安全直接实现

Eval 不能：

- 只调用 classifier 后自行执行命令；
- 直接实例化 runtime Terminal/Bash tool；
- 使用 permission bypass 模式；
- 在无用户环境中自动批准 ask；
- 通过裸 subprocess 绕过 Mote 权限和 sandbox。

classifier 只负责分类，不等于完整授权、审批、sandbox 和执行生命周期。

### 5.2 Product 需要提供正式命令服务

在 `CommandEvaluator` 进入实现前，Product 必须暴露一个受策略约束的命令执行服务：

```python
class PolicyBoundCommandService(Protocol):
    async def execute(self, request: ScopedCommandRequest) -> CommandReceipt: ...
    async def cancel(self, execution_id: str) -> CancellationReceipt: ...
```

该服务必须保证：

- argv 序列执行，不经过 shell；
- deny 永远不可绕过；
- headless ask 默认转换为 deny；
- 禁止自动批准和权限升级；
- cwd 固定在 attempt workspace；
- readable/writable roots 固定在 case scope；
- 经过完整 permission policy/pipeline；
- 使用正式 sandbox；
- timeout/cancellation 清理整个进程组；
- stdout/stderr 超限时发布 ArtifactRef；
- 返回 policy decision 和 execution receipt。

在该门面完成前，Eval 第一阶段只能实现不执行命令的确定性 evaluator，例如文件存在、内容匹配、JSON 和 changed-files 检查。

## 6. Artifact 契约的收口意见

### 6.1 主设计当前存在重复风险

主设计文档描述了 Eval 自己的 `ArtifactRef`，但仓库已有 `contracts.artifact.ArtifactRef`。如果两套契约同时实现，会产生：

- 两套 artifact identity；
- 两套 retention/sensitivity；
- adapter 层重复转换；
- report、session 和 Eval 对 artifact 状态解释不一致；
- 未来迁移和兼容负债。

### 6.2 建议的最终边界

Eval 应复用 `contracts.artifact.ArtifactRef` 作为跨层 artifact 引用，并只定义 Eval 自己的实验归属信息：

```python
@dataclass(frozen=True, slots=True)
class EvalArtifactRecord:
    artifact: ArtifactRef
    experiment_id: ExperimentId
    case_id: CaseId | None
    run_id: RunId | None
    attempt_id: AttemptId | None
    producer_phase: EvalPhase
    producer_id: str
    integrity_observation: ArtifactIntegrityObservation
```

边界如下：

```text
contracts.artifact.ArtifactRef
  负责 artifact 的稳定引用、内容、retention、sensitivity 和 publication。

eval.domain.EvalArtifactRecord
  负责 artifact 在实验中的来源、归属和观察状态。
```

这会要求修订主设计中“`eval/domain` 只依赖 stdlib”的绝对规则。建议允许 `eval/domain` 依赖 `contracts` 中经过批准的稳定数据契约，但仍禁止依赖 runtime、orchestration 和 product。

推荐依赖规则调整为：

```text
eval/domain          -> stdlib + contracts 的批准稳定 DTO
eval/application     -> eval/domain
eval/infrastructure  -> eval/domain + eval/application
eval/adapters/mote   -> eval/domain + eval/application + product 公开 API
```

批准清单必须由 architecture test 固化，不能开放成对整个 `contracts` 的任意依赖。

## 7. CaseExecutor 与进程隔离的当前边界

Mote 当前有 Agent/Engine 生命周期，但没有 Eval CaseExecutor，也没有如下实验语义：

- CaseExecutionRequest；
- ExecutionLease；
- AttemptId 幂等；
- worker heartbeat；
- runner 重启后的 reconcile；
- executor lost；
- fresh fixture materialization；
- attempt-scoped process group；
- execution receipt codec。

因此 LocalProcessCaseExecutor 必须作为新的 Eval infrastructure 实现建设，不能把现有 Engine 直接称为 CaseExecutor。

二者关系应是：

```text
LocalProcessCaseExecutor
  拥有 attempt worker 进程与 execution lease
      ↓
Mote headless Product facade
  拥有 worker 内 Application/Engine/Agent 生命周期
```

外层 executor 负责杀死失控 worker 和对账；内层 Product facade 负责正常释放 Mote 资源。两层不能互相替代。

## 8. 当前可以直接复用与不能直接复用的能力

| 能力 | 结论 | 使用方式 |
| --- | --- | --- |
| Product Application | 可复用 | 作为 headless facade 内部生命周期根 |
| Engine ownership/cleanup | 可复用 | 作为 worker 内 Agent 所有权机制 |
| CodingAgentFactory | 可复用 | 只能由 Product composition 调用 |
| Role.run | 可复用 | 作为 MoteCodingTask 内部执行入口 |
| Session rollout/replay | 可参考并关联 | 不作为 Eval journal |
| contracts ArtifactRef | 应直接复用 | Eval 只补实验归属记录 |
| PermissionEngine | 通过 Product 门面复用 | Eval 不直接组装 runtime engine |
| CLI build_engine | 不可直接复用 | 应下沉共享 Product facade 后由 CLI/Eval 共用 |
| Terminal/Bash tool | 不可直接复用为 evaluator | 需 Product PolicyBoundCommandService |
| multiprocessing/subprocess | 不能直接视为 CaseExecutor | 必须补 lease/reconcile/receipt/ownership |

## 9. `adapters/mote` 的含义与收口

`adapters/mote` 不是一个新的框架层，也不是允许 Eval 随意访问 Mote 内部实现的目录。它只负责把 Product 已公开的能力投影为 Eval application ports：

```text
EvalTask / CommandEvaluationService / TaskLifecycle 等 Eval 端口
                           ↑
                  eval/adapters/mote
                           ↑
               Product 公开 request/receipt API
```

它可以做：

- 把 `CodingTaskInput` 转换为 Product headless request；
- 把 Product typed receipt 转换为 `TaskResult` 和 Eval provenance observation；
- 把 Product 公开 artifact 引用关联到 experiment/run/attempt；
- 把 Product 的 cancel/reconcile/close receipt 映射成 Eval 状态机事实。

它不可以做：

- 构造 `RoleSchema`、`RoleState`、`AgentWiring`、`EngineServices` 或 `AgentControl`；
- 读取 Role 私有属性补齐 Product receipt 缺失字段；
- 直接实例化 runtime permission engine、Terminal/Bash tool 或 session store；
- 自己实现一份 CLI bootstrap；
- 将 Product 内部对象引用放入可持久化 Eval result。

若团队认为 `adapters/` 术语过于抽象，也可以将目录命名为 `eval/coding/`。名称不是架构要求；唯一硬约束是 Mote-specific 依赖集中在一个边界，并且该边界只消费 Product 正式 API。主设计、测试路径和 public export 必须统一选择一种名称，不能同时保留 `coding/` 与 `adapters/mote/` 两套入口。

## 10. 资源所有权必须闭合

当前主设计已经列出大量资源，但仍需用单一所有权树消除 runner、lifecycle、executor 和 Product facade 之间的职责重叠：

```text
ExperimentRunner
└── CaseExecutor（拥有 execution lease/worker）
    └── AttemptScope
        ├── WorkspaceLease（由 WorkspaceManager 创建和回收）
        ├── TaskLifecycle
        │   └── ProductApplicationLease（拥有 Agent/session/Product resources）
        ├── EvaluatorInvocation
        │   └── EvaluatorArtifactScope
        └── JournalFactSink
```

必须明确：

- runner 只编排，不直接创建 Role、进程或 workspace；
- CaseExecutor 是 attempt worker 和 timeout/cancel/reconcile 的唯一 owner；
- WorkspaceManager 是 workspace 创建、物化、保留和删除的唯一 owner；
- CaseLifecycle 不创建或删除 workspace，只管理 fixture/setup/prepare/teardown 资源；
- MoteCodingTask 只拥有它通过 Product facade 获得的 application/session lease；
- Evaluator 不能清理 case 资源，只能关闭自己显式获得的 scoped capability；
- 每个 owner 都必须产生可 codec 化的 close/cleanup receipt；`finally` 执行过不等于资源已成功释放。

没有这棵所有权树，超时路径会出现多个组件都尝试清理同一进程，或相互认为对方负责清理的长期缺陷。

## 11. Retry、Resume、Restart 与 Repair 必须分离

四种操作不能共用一个模糊的“重新运行”入口：

| 操作 | 身份语义 | 允许执行的动作 |
| --- | --- | --- |
| retry | 同一 RunId，新 AttemptId | 根据 durable `RetryScheduled` 创建 fresh attempt |
| resume | 同一 ExperimentId | replay 后 reconcile 非终态 lease，只推进合法 transition |
| restart | 新 ExperimentId | 复用声明输入，不继承旧实验状态 |
| repair | 同一事实历史上的投影/Artifact 修复 | 不重新执行 task/evaluator，除非显式策略授权 |

默认 retry 必须物化全新 workspace、session、Agent scope 和进程组。复用 workspace 是显式 continuation policy，不得仍标记为隔离 retry，也不得默认与 fresh retry 直接比较。

恢复器还必须遵守：

- `started` 且无 terminal receipt 的外部执行先 reconcile，不能直接 retry；
- evaluator 已 durable completed 后不得因 coordinator 崩溃再次执行；
- `CaseResultFinalized` 后只允许验证摘要并补交 `CaseCompleted`；
- Artifact 缺失默认产生 integrity observation，不修改原始成功事实；
- retryability 是稳定错误分类和 policy 的联合结果，不由异常 message 猜测。

## 12. 状态、事实与报告边界

主设计采用 execution/evaluation/cleanup/completion 正交状态是正确方向，但实现前还必须确定以下不变量：

1. event payload 只陈述已发生事实，不直接携带可任意覆盖的聚合 `CaseResult`。
2. `CaseCompleted` 是正常 case 的唯一 terminal commit，并引用 finalized result digest。
3. cleanup failure 不覆盖 task/evaluator 事实；实验是否因此失败由版本化 ExperimentPolicy 决定。
4. `skipped`、`timed_out`、`cancelled`、`executor_lost` 都必须有明确事实，不能从缺少 completed 事件推断。
5. live runner、resume、`show`、report 和 compare 只消费同一个 reducer projection。
6. reducer 遇到非法 transition、sequence gap 或中间损坏时 fail closed，不能尝试“尽量猜出结果”。

建议在写 runner 前先完成状态转移表。每一个 `(state, event) -> state` 都应有测试；没有合法 transition 的事件必须拒绝提交或使投影进入明确 integrity failure。

## 13. 指标、Provenance 与比较边界

长期可比较性依赖稳定语义，不依赖同名字符串。实现前必须闭合：

- evaluator ID、非空版本和 config digest；
- metric ID、kind、unit、better direction、aggregation 和 definition digest；
- dataset/case/task/fixture/codec/report-policy digest；
- Product facade、Mote revision、toolset/skill/MCP、permission/sandbox policy；
- requested route 与实际 provider/model/fallback observation；
- isolation、retry workspace、durability 和 executor policy。

比较器的首要输出应是 compatibility decision：`comparable`、`conditionally_comparable` 或 `not_comparable`，之后才是数值差异。同名 dataset、case 或 metric 不能覆盖 digest/schema 不一致。

“完全可复现”也不能成为布尔字段。外部模型服务无法冻结时，最多是输入完整、外部变量已识别的 bounded reproducibility；报告必须展示等级和缺口。

## 14. 主设计文档当前需要同步修正的具体问题

当前 `EVAL_PACKAGE_SKELETON.md` 已经吸收多数架构意见，但仍有几处在编码前应修正：

1. `eval/domain` 被规定只依赖 stdlib，却又在 domain 内重新声明 ArtifactRef；应改为只依赖 stdlib 加经批准的 `contracts.artifact` DTO，并删除第二套通用 ArtifactRef。
2. `eval/adapters/mote` 的 Product/Contracts 依赖白名单需要列出具体 public modules，并由 architecture test 固化；“公开 API”不能只靠约定。
3. `Case` 示例重复声明 `expected_output`，应删除重复字段。
4. retry 的 FRESH 策略文本重复“新 session、新 Agent execution scope、新进程组”，应清理重复内容。
5. ArtifactStore 与现有 Runtime artifact store 的关系尚未说明：Eval 可以拥有实验级 store implementation，但 durable reference 必须继续使用唯一 Contracts ArtifactRef。
6. Product facade request/receipt 应先进入 `contracts` 还是只由 Product 导出，需要按跨边界消费方决定并记录 ADR；Eval 不应定义 Product receipt 的镜像 DTO。
7. `JournalFactSink` 的 worker-to-coordinator transport、背压、coordinator 丢失和 acknowledgment 重连语义仍需形成端口，而不是仅写在事件规则中。
8. `InProcessCaseExecutor` 只能用于 trusted/低风险工作，第一阶段 Coding Agent 的验收必须以 `LocalProcessCaseExecutor` 为准。

这些问题不否定主设计方向，但如果不先修正文档，会直接变成重复契约、架构白名单漂移和恢复语义空洞。

## 15. 源码证据矩阵

本评审的关键判断应绑定当前源码事实，后续代码演进时据此重新验证，而不是把本文件当作永久真相：

| 判断 | 当前源码证据 | 对 Eval 的含义 |
| --- | --- | --- |
| Product 低级 application builder 仍要求调用方理解 object graph | `product/composition/bootstrap.py::build_application` 接收 `ProductContainer`、`EngineServices` 和 `agent_factory` | 它不是可直接交给 Eval 的 headless facade |
| CLI 已有完整但带交互语义的装配 | `product/entrypoints/cli/bootstrap.py::build_engine` | 应抽取共享 Product facade，不能让 Eval import CLI |
| CodingAgentFactory 暴露的是 Product 内部构造输入 | `product/agents/factory.py::RootAgentRequest` 包含 `RoleSchema`、`RoleState`、`AgentWiring` | Eval 不应消费或复制该 request |
| Engine 已有取消安全、幂等 close 与 Agent ownership | `runtime/engine.py::Engine.release/aclose/_close` | 可作为 Product facade 内部资源根，但没有 Eval receipt/reconcile 语义 |
| Role.run 返回类型化且 durable committed 的 outcome | `runtime/agent/role.py::Role.run` | Product facade 应投影该结果，不需 Eval 读取 Role 私有状态 |
| ArtifactRef 已是 Contracts 稳定 DTO | `contracts/artifact/models.py::ArtifactRef` | Eval 不应重复声明通用引用；只补实验归属与完整性 observation |
| PermissionEngine 会把 ask 收敛为 terminal decision | `runtime/tools/permission/engine.py::PermissionEngine.check` | Product command service 应复用完整管线，而非 classifier |
| 默认组装存在 bypass 风险 | `runtime/tools/policy.py::build_tool_call_policy` 在仅 `require_permission=True` 时构造 `PermissionConfig(mode="bypass")` | Eval 必须提供显式 headless policy，不能依赖隐式默认值 |

实现 PR 若改变上述事实，应同时更新本评审、主设计和相应 ADR；否则评审会快速退化成过时文档。

## 16. 必须产出的 ADR

以下决策影响跨包依赖或 durable schema，不能只埋在实现代码中：

### ADR-EVAL-001：Eval 在现有五层之外的依赖地位

确定 `eval/domain` 对 Contracts DTO 的批准清单、Mote-specific adapter 的唯一位置，以及 architecture test 规则。

### ADR-EVAL-002：Product headless application facade

确定 request/receipt/cancel/reconcile/close 的属主、版本策略、CLI 迁移方式和人机交互默认策略。

### ADR-EVAL-003：Artifact 单一身份与实验归属

确定 `contracts.artifact.ArtifactRef` 的复用方式、EvalArtifactRecord、实验级 store、retention 和完整性 projection。

### ADR-EVAL-004：Experiment journal durability 与迁移

确定 envelope、单写者、fsync policy、torn tail、幂等事实、schema migrator 和 projection invalidation。

### ADR-EVAL-005：CaseExecutor lease 与进程隔离

确定 worker transport、heartbeat、cancel、reconcile、executor-lost、进程组和 coordinator crash 行为。

### ADR-EVAL-006：Retry/Resume/Restart/Repair

确定身份保持、允许 transition、fresh workspace 默认、selected attempt 和外部副作用对账。

### ADR-EVAL-007：Policy-bound command evaluation

确定 Product command service 的权限、sandbox、headless ask、argv、输出 artifact 和进程清理语义。

这些 ADR 应在对应 production type 合入前完成，而不是实现结束后补写。ADR 只记录长期决策与被否决方案，不复制主设计全文。

## 17. P0 风险登记

| 风险 | 触发方式 | 后果 | 关闭条件 |
| --- | --- | --- | --- |
| 双 ArtifactRef | 直接实现主设计中的 Eval ArtifactRef | session/Product/Eval 无法共享引用和策略 | 删除重复 DTO，compat tests 使用 Contracts ArtifactRef |
| CLI 成为事实公共 API | MoteCodingTask import CLI bootstrap | 交互默认值与私有装配永久外泄 | CLI/Eval 均迁移到 Product headless facade |
| 权限旁路 | CommandEvaluator 只调用 classifier 或 subprocess | deny/ask/sandbox 可被绕过 | 仅注入 Product policy-bound service |
| 脏 workspace retry | attempt 复用前次文件和进程 | 指标不可比较、失败被掩盖 | FRESH 为默认且 fixture digest 一致 |
| coordinator 双执行 | resume 未 reconcile lease 就 retry | Agent/命令外部副作用重复 | durable lease + reconcile-before-retry 测试 |
| cleanup 假成功 | `finally` 返回即记 succeeded | Terminal/后台进程跨 case 泄漏 | typed CleanupReceipt + 资源枚举验证 |
| journal 假 durable | 仅 flush 或多 worker 直写 JSONL | 崩溃后事实丢失/交错 | 单写者 committer + durability policy + crash tests |
| 伪可比较 | 只按名称/version 比较 | 不同配置或指标语义混算 | digest/schema compatibility gate |
| in-process 假隔离 | Coding Agent 默认跑在线程/同进程 | timeout 无法强制回收，全局状态污染 | LocalProcessCaseExecutor 成为 Coding 默认 |
| adapter 膨胀 | adapter 开始组装 runtime/private Role | Eval 变成新的 Product 旁路 | import gate + Product receipt 完整性测试 |

风险关闭必须由测试或 architecture gate 证明，不能仅把状态改为“已知风险”。

## 18. 最小可开工切片

在 Product facade 和 LocalProcessCaseExecutor 尚未完成时，可以先实现一个不会固化错误边界的 vertical slice：

```text
JSON-native Dataset/Case
→ trusted AsyncFunctionTask
→ InProcessCaseExecutor（明确标记 weak isolation）
→ 一个 assertion evaluator
→ versioned journal committer
→ pure reducer/replay
→ JSON report
```

该切片必须满足：

- 使用正式 Experiment/Case/Run/Attempt identity；
- 所有 durable value 经过 JsonValueCodec；
- 使用 Contracts ArtifactRef 或完全不产生 artifact，不能临时定义替代类型；
- 具备 fresh workspace，即使纯函数任务暂时不用；
- `CaseCompleted` 在 teardown receipt 后提交；
- 支持在 task/evaluator/teardown 边界注入真实子进程崩溃并 replay；
- provenance 明确记录 executor 为 in-process、隔离等级为 weak；
- 不实现 Coding Agent、CommandEvaluator、CLI bootstrap adapter 或任意 Python plugin loader。

这个切片的目标是验证 durable domain，而不是展示 benchmark 功能。它通过后，Product facade 与 process executor 可以作为既有端口的新实现接入，不需要改 Dataset、Metric、Event 或 Report 契约。

## 19. 并发、背压与公平性边界

`max_concurrency` 不能只实现为 runner 外层一个 Semaphore。Eval 至少存在四类独立容量：

```text
case admission capacity
worker process capacity
model/provider capacity
evaluator/command capacity
```

如果只限制活跃 case，一个 case 内的多 Agent、后台任务、命令 evaluator 和模型调用仍可能突破全局资源上限。反之，直接复用 Product 内部模型 bulkhead 也不能替代 Eval case admission。

第一版需要明确：

- coordinator 在创建 workspace/worker 前取得 case admission permit；
- permit 属于 execution lease，直到 worker terminal 且 cleanup receipt 已提交后释放；
- 排队不算 task duration，但计入 queue duration 和 experiment wall time；
- queue deadline、task deadline、evaluator deadline 和 experiment deadline 分开记录；
- fail-fast 停止新 admission，但不把已提交事实回滚；
- cancellation 优先于新 retry，已 durable terminal 结果不被迟到的 cancel 覆盖；
- worker fact channel 必须有界，heartbeat/terminal/lease 事实不能被普通进度事件饿死；
- report 顺序由 dataset/run identity 决定，不由并发完成顺序决定。

公平性策略属于版本化 ExperimentPolicy。首版可使用稳定 FIFO，但不能依赖 asyncio task scheduling 的偶然顺序。未来引入按 dataset、provider 或租户的加权公平时，应只替换 admission policy，不改变 Case 状态机。

## 20. Deadline、时钟与取消语义

持久化跨进程 deadline 不能只保存某个进程的 monotonic timestamp；展示和恢复也不能只依赖 wall clock。建议区分：

```text
declared timeout      持久化的 duration policy
wall-clock deadline   跨进程/恢复使用的 UTC aware timestamp
monotonic start       单进程内测量 elapsed time
```

Mote 已有 `contracts.inference.CrossProcessDeadline` 可作为设计参考，但 Eval deadline 是否复用它需要进入 Contracts 批准清单，不能直接依赖 Runtime 实现。

取消规则必须固定：

1. coordinator durable commit cancel intent；
2. executor 根据 lease 请求 cooperative cancellation；
3. grace period 到期后终止 worker process group；
4. reconcile 实际进程状态和已有 terminal receipt；
5. durable commit cancellation receipt；
6. 进入 teardown/finalization。

若 terminal receipt 先于 cancel intent durable commit，保留 terminal 结果；若 cancel intent 已提交而迟到成功结果到达，结果作为 late receipt 保存，但默认不选择为 run result。该冲突必须由 reducer 规则处理，不能由竞态发生顺序临时决定。

测试应注入可控 clock，不应通过真实长时间 sleep 验证 deadline。时间戳用于审计，duration 使用 monotonic measurement；系统时钟回拨不能产生负 duration。

## 21. Budget、Token 与 Cost 边界

并发上限不是预算。Eval 需要独立的版本化 BudgetPolicy，至少支持：

- experiment/case/run/attempt 最大 wall time；
- 最大 task/evaluator attempts；
- token、货币成本和模型调用数上限；
- artifact bytes 和 journal bytes 上限；
- worker/process 数及命令执行数上限。

Mote 当前已经有 inference budget/usage ledger 与 CostTracker 能力，但 Eval 不应直接读取 Runtime tracker 私有状态。Product headless receipt 应返回 Contracts 级 usage/cost observation，并标明：

- requested/reserved/settled 三种状态；
- provider usage 是否 authoritative、estimated 或 unavailable；
- cache/reasoning/input/output token 分类；
- currency、pricing version 和计算时间；
- fallback/重试产生的全部调用，而不是只统计 selected attempt 的最后一次调用。

Budget exceeded 是明确终态事实，不应伪装成普通 timeout 或 task error。已发生但尚未 settlement 的成本进入 pending reconciliation，report 不得将其当作零。

比较报告必须区分：

```text
selected-attempt quality metrics
all-attempt resource consumption
```

否则 retry 可以通过只展示成功 attempt 的成本，系统性低估实验代价。

## 22. Secret、脱敏与数据治理边界

“报告默认脱敏”不足以覆盖 journal、artifact、traceback 和命令输出。脱敏必须发生在每个 durable boundary 之前，并保留可审计 observation：

- dataset 声明禁止内嵌 secret，只保存命名引用；
- Product facade 负责解析 secret，Eval adapter 永远不接收明文 credential；
- worker 日志、task output、evaluator reason、error message、traceback、stdout/stderr 在持久化前经过同一 redaction capability；
- redaction failure 对可能含 secret 的内容 fail closed，不能原样写盘；
- digest 不直接充当 secret fingerprint；fingerprint 必须带域分离和受控不可逆策略，避免低熵 secret 被离线枚举；
- secret artifact 默认不可进入终端/JSON report，只显示受控 metadata；
- compare 不解析 secret artifact 内容；
- retention 到期需要可审计删除结果，但删除投影不能篡改历史 artifact-created 事实。

现有 Product/Runtime 已有 secret handle、vault 和 redaction primitives。Eval 应通过 Product 公开能力复用其策略，不直接 import `runtime.secrets` 形成新的 secret 读取路径。

还应显式区分三种数据等级：dataset 输入、被测 workspace、实验 artifact。三者的 retention、publication 和访问策略不同，不能因为都位于 experiment root 就获得相同权限。

## 23. 公共 API 与兼容策略

首版不能把所有 dataclass 都视为十年稳定 API。建议定义三个兼容面：

| 兼容面 | 承诺 | 演进方式 |
| --- | --- | --- |
| Python authoring API | source-compatible 的 Dataset/Task/Evaluator/Metric 构造接口 | 语义版本、弃用周期、类型兼容测试 |
| Durable wire schema | journal event、execution request/receipt、result、provenance | 显式 schema version 和 migrator，禁止 pickle |
| Plugin registry schema | YAML 中 `(kind, stable_id, version)` 与 config codec | 精确版本解析，不自动 latest |

内部 runner、worker transport、projection cache 和 adapter implementation 不属于 public API。以下划线命名不够，必须由显式 export 清单、import architecture test 和文档共同约束。

兼容策略建议：

- durable event payload 一旦发布不得改变已有字段语义；新增可选观察优先新增事件类型；
- 删除或重命名 metric ID 视为新 metric definition，不做静默 alias；
- evaluator version 变化不代表自动不可比，最终由 config/metric/provenance compatibility rule 判断；
- migrator 必须是确定性纯转换，保存 source digest 和 migrator chain；
- 旧 reader 遇到未知状态转换事件 fail closed，旧 reader 遇到声明为 ignorable 的纯 observation 才可继续；
- projection/reporter 版本可独立升级，因为它们不是真相源。

## 24. Evaluator 确定性与副作用边界

“确定性 evaluator”不能只表示“不调用 LLM”。它至少要求相同的 codec 输入、workspace snapshot、evaluator config 和实现版本产生相同的 MetricObservation。以下因素都会破坏确定性：

- 读取当前时间、随机数、locale、timezone 或进程环境；
- 枚举文件时依赖文件系统返回顺序；
- regex/JSON/schema 库版本漂移；
- 命令 evaluator 使用外部网络、共享 cache 或未固定依赖；
- evaluator 修改 workspace 后影响后续 evaluator；
- evaluator 顺序变化改变结果。

每个 evaluator descriptor 应声明：

```text
determinism: deterministic | environment_bounded | nondeterministic
workspace_access: none | read_snapshot | command_service
network_access: none | declared
side_effects: none | artifact_only | external
isolation_requirement: in_process | process | sandbox
```

执行规则：

- 文件 evaluator 读取 task 完成后生成的同一个 sealed workspace snapshot，而不是各自在 live workspace 中读取；
- evaluator 默认可以并行，但只有在 capability manifests 无冲突且输入 snapshot 相同时才允许；否则按声明顺序执行；
- evaluator artifact namespace 独立，不能通过共享目录通信；
- evaluator 失败不能改变其他 evaluator 的输入；
- CommandEvaluator 的命令、环境白名单、工具链版本和输出 codec 都进入 config digest/provenance；
- nondeterministic evaluator 可以运行，但 report 必须标记，默认不能用单次 observation 宣称回归。

未来 LLM Judge 仍必须遵守该 descriptor，不得因其天然非确定而绕过版本、seed、model/provider、prompt digest 和重复采样记录。

## 25. Fixture、Git 与 ChangedFiles 边界

Coding Eval 的真实输入不是一个 `Path`，而是一个不可变 FixtureSnapshot。该 snapshot 至少包含规范化 tree manifest：

```text
relative path
entry kind
content digest（regular file）
executable bit
symlink target（若策略允许）
```

必须先决定并固化：

- 是否保留 symlink；允许时只能使用词法目标还是必须解析后验证 scope；
- 是否保留 executable bit；
- 是否忽略 mtime、uid/gid、xattr 和平台特有 metadata；
- filename Unicode normalization 与大小写冲突策略；
- 空目录是否进入 digest；
- `.git` 是 fixture 内容、独立 baseline metadata，还是默认排除；
- 文件数量、单文件大小和总字节上限。

`ChangedFilesEvaluator` 不能只调用当前 git diff：

- fixture 可能不是 git repo；
- task 可能删除或重命名文件；
- task 可能修改 `.gitignore`；
- untracked/ignored 文件是否计入需要显式定义；
- git autocrlf、filemode 和平台配置会改变结果。

建议 changed-files 使用 FixtureSnapshot 与 final sealed snapshot 的内容寻址 diff 作为权威事实，Git 只作为可选辅助 artifact。结果应区分 added/modified/deleted/renamed/type_changed；rename 是投影启发式，不应改变底层 delete+add 事实。

现有 Runtime file snapshots 和 VCS collector 可以提供设计参考，但 Eval fixture 是 experiment input，不能直接复用 session undo snapshot 或 best-effort git status 作为其身份来源。

## 26. 失败分类与重试判定

异常类名不足以决定 retry。建议每个失败事实同时携带正交分类：

```text
phase
origin: eval | task | evaluator | product | provider | executor | infrastructure
category: validation | policy | capacity | timeout | cancellation | unavailable |
          integrity | protocol | user_code | external_unknown | internal_bug
retry_disposition: never | same_attempt_reconcile | fresh_attempt | operator_decision
effect_certainty: none | not_started | completed | unknown
```

关键规则：

- validation、policy deny、codec/schema incompatibility 默认不可 retry；
- capacity/unavailable 可以 fresh retry，但受 deadline 和 budget 约束；
- timeout 不天然可 retry，必须先确认旧 lease 已终止或 reconcile；
- cancellation 永远不触发自动 retry；
- executor_lost 且 effect certainty unknown 进入 operator/recovery policy，不能静默重跑；
- evaluator failure 只重试该 evaluator，不重新执行 task，除非其输入 artifact 已损坏；
- internal_bug 应保留完整诊断并使实验 policy 可选择 fail-fast，不能无限 retry。

用户可读 message、provider 错误字符串和 traceback 只用于诊断，不能参与状态或 retry 分支。

## 27. Eval 事实与 Observability 分离

Eval journal 与 telemetry/log/trace 是两条不同平面：

```text
durable fact plane
  决定恢复、状态、报告和比较；有严格 schema、顺序、ack 和 retention。

observability plane
  用于调试、性能分析和 UI 实时反馈；允许采样、丢弃和不同 retention。
```

不得用 log line 或 telemetry 是否出现来判断 task/evaluator 完成。反过来，token streaming、普通进度和高频 heartbeat 也不应全部写入 durable journal 导致无限增长。

建议：

- 每个 durable event 带 trace correlation ID，但 reducer 不依赖 trace backend；
- journal commit latency、queue wait、worker startup、cleanup latency 作为 operational metrics；
- telemetry overflow 不改变实验事实，但必须暴露 diagnostic；
- heartbeat 采用有界/可压缩的 lease observation，journal 只保留恢复所需 checkpoint；
- exception logging 遵守仓库 `@log_class` 约定，Eval 状态转换不靠手写 inline logger；
- report 展示业务评测事实，诊断包单独包含 redacted logs/traces/artifacts。

## 28. 故障注入与确定性测试矩阵

仅测试 Python exception 不足以证明 durable eval。每个关键窗口至少覆盖进程退出、I/O 失败和取消竞态：

| 窗口 | 注入 | 必须结果 |
| --- | --- | --- |
| lease 已提交、worker 未启动 | coordinator crash | resume reconcile 为 not-started 或 lost，不重复 lease |
| setup 副作用后、receipt 前 | worker kill | effect unknown，按 operation ID reconcile，不盲目 setup |
| task 成功 receipt 已发布、fact 未 ack | coordinator/transport failure | 同 event ID 重交并幂等 commit |
| evaluator artifact 已发布、completed 未提交 | worker kill | integrity/recovery policy 决定 repair，不直接重复副作用 evaluator |
| teardown 部分释放 | worker kill | 根据 resource manifest 继续 reconcile，原 task 结果保留 |
| CaseResultFinalized 后、CaseCompleted 前 | coordinator crash | 验证 digest 后补交 terminal event |
| journal 尾部半写 | process kill | 忽略 torn tail 并产生 diagnostic |
| journal 中段损坏 | bit corruption | replay fail closed，不越过损坏记录 |
| cancel 与 success 同时到达 | deterministic scheduler | 按 durable order/transition rule 得到唯一结果 |
| budget exhausted 与 retry 同时发生 | deterministic scheduler | 不创建未授权新 attempt |

另外需要 property/model-based tests：

- 任意合法事件序列 live reduce 与 replay 等价；
- 任意非法 transition 被拒绝；
- 重复同一事实不改变最终 projection；
- case 顺序和结果序列化不受并发完成顺序影响；
- fresh retry 的初始 fixture digest 始终一致；
- redaction 后 durable payload 不包含已知 secret corpus；
- migration chain 确定且重复执行结果一致。

故障测试生成的 worker、session、Terminal/Python 和临时目录必须使用唯一 identity 并显式 cleanup，避免测试自身制造 singleton 泄漏。

## 29. 核心术语的唯一含义

以下术语必须在 Python API、event、磁盘布局、CLI 和报告中保持一致，禁止模块各自解释：

| 术语 | 唯一含义 | 明确不是 |
| --- | --- | --- |
| Experiment | 一次不可变声明快照下的完整评测执行 | dataset 本身或 report 文件 |
| Dataset | 有稳定身份和摘要的 case 声明集合 | 一次运行状态 |
| Case | dataset 中一个逻辑评测样本 | repeat 或 retry |
| Run | 一个 Case 的一次 repeat | task method 的单次调用 |
| Attempt | Run 下由 retry policy 授权的一次 task execution | resume 后的每次进程启动 |
| Evaluation | 一个 evaluator 对 selected task result 的一次执行 | metric 本身 |
| MetricObservation | evaluator 产生的一条不可变测量事实 | 聚合值或总分 |
| FixtureSnapshot | attempt 开始前的不可变工作区输入 | live workspace |
| Workspace | 某个 attempt 的可变执行目录 | artifact store 或 fixture source |
| ExecutionLease | executor 对 attempt worker 的取消/对账 authority | Agent session lease |
| Receipt | 外部操作可持久化、可对账的结果证明 | 普通函数返回值的别名 |
| Journal | experiment 编排事实的 append-only 真相源 | log、telemetry 或 projection |
| Projection | 从 journal 重建的可删除派生状态 | 可修改真相源 |
| Resume | 恢复同一 ExperimentId 并推进既有状态机 | 创建新实验或无条件重跑 |
| Retry | 同一 RunId 下创建新 AttemptId | 复用旧 workspace 的隐式续跑 |
| Repeat | 同一 case 声明下创建新 RunId | retry |
| Repair | 修复投影、索引或 artifact 完整性 | 默认重新执行 task/evaluator |

代码命名应服从这张表。例如公共门面应叫 `ExperimentRunner`，而不是容易把 dataset 声明与 experiment execution 混为一谈的 `DatasetRunner`。若为了用户易用保留 `DatasetRunner`，它只能是薄别名，durable model 仍使用 Experiment/Run/Attempt 术语。

## 30. 主设计处置矩阵

对 `EVAL_PACKAGE_SKELETON.md` 的当前内容建议按以下方式处置：

| 设计项 | 处置 | 原因 |
| --- | --- | --- |
| Eval 是最外层应用、五层禁止反向依赖 | 接受 | 符合 Mote 单向分层 |
| Mote-specific adapter 集中依赖 Product | 接受并收紧 | 需要 public module 白名单和唯一目录命名 |
| domain 只依赖 stdlib | 修订 | 应允许批准的 Contracts DTO，尤其 ArtifactRef |
| Eval 自定义通用 ArtifactRef | 拒绝 | 与现有 Contracts ArtifactRef 重复 |
| identity/digest/version 优先 | 接受 | 是恢复和比较基础 |
| codec 禁止 pickle | 接受 | 跨进程与长期迁移必须显式 |
| 默认 fresh-attempt workspace | 接受 | retry 可比较性的必要条件 |
| 正交 execution/evaluation/cleanup/completion 状态 | 接受 | 防止单一 status 丢事实 |
| journal/reducer 为唯一事实路径 | 接受 | 避免 live/replay/report 漂移 |
| Product headless facade | 接受但先独立 ADR | 需要与 CLI 共同迁移，不能 eval-only |
| Policy-bound CommandEvaluator | 接受但延后启用 | Product service 未完成前禁止旁路实现 |
| InProcess 与 LocalProcess executor 首版同时完整交付 | 缩减 | 先用 InProcess 验证 durable core，再以 LocalProcess 作为 Coding gate |
| heartbeat/remote-ready lease 全功能首版实现 | 缩减 | 本地需要 cancel/reconcile，远程租约续期细节后置 |
| 全量 provenance 首版强制 exact | 修订 | 采用 grade；缺失项可运行但不可伪称完全复现 |
| dataset evaluator | 延后 | 与 case evaluator 生命周期不同，需要独立 ADR |
| LLM Judge/视觉/分布式 | 延后 | 只能实现既有端口，不进入首版核心 |

“缩减”不表示删除端口，而是冻结最小语义后只实现当前场景需要的能力。未来字段不得以 `reserved`, `extra` 或任意 dict 提前占位。

## 31. 复杂度预算与反过度设计规则

十年低负债不等于首版实现十年内所有功能。首版只应为已经确定的变化轴建立端口：

```text
已确定变化轴：
executor（in-process/local-process）
task/evaluator 实现
codec
report policy
journal store
Mote Product adapter

尚未确定变化轴：
远程调度协议
租户/配额模型
Web API/UI 查询模型
第三方 eval 格式
container/cloud artifact transport
LLM Judge sampling framework
```

反过度设计规则：

- 没有第二个真实实现或明确近期需求的接口，优先使用内部具体类而不是公共 Plugin Protocol；
- durable schema 只记录当前事实，不添加 “future metadata” 任意字典；
- 不预留任意 provider payload；原始响应需要保存时进入 typed artifact；
- LocalProcess transport 可以是本机受控 IPC，不为假设中的远程 worker 定义网络协议；
- heartbeat 首版只解决本地进程存活与 coordinator recovery，不设计租约续租集群共识；
- migration framework 首版只需证明 v1→测试 v2 的链路，不提前实现通用 schema DSL；
- registry 首版只注册内置类型，不加载 entry point 或用户插件；
- report 首版只做声明过的聚合，不建立查询语言；
- 所有“后续可扩展”必须指出现有 seam，不能通过空基类、generic utils 或未消费字段表达。

每增加一个公共 Protocol、durable event 或 identity 类型，都必须回答：当前有哪两个调用方/实现需要它、它保护了什么不变量、删除它会造成什么具体耦合。答不出来则保持内部实现。

## 32. 关键路径与可并行工作

真正阻塞关系如下：

```text
Artifact/Contracts 决策 ─┐
Identity/Codec/Metric ───┼→ Event/Reducer/Journal → Runner/Recovery
状态转移表 ──────────────┘                         │
                                                   ├→ Report/Compare
Product headless facade → Mote adapter ────────────┤
Workspace/Executor port → LocalProcess executor ──┤
Product command service → CommandEvaluator ───────┘
```

可以并行：

- Product 团队设计 headless facade；
- Eval 团队完成 identity/codec/metric/state reducer；
- Artifact 决策完成后建设 experiment artifact attribution；
- durable core 稳定后并行开发 report 和 process executor。

不能并行抢跑：

- Artifact 决策前实现 Eval artifact store DTO；
- 状态转移表前实现 runner 分支；
- execution receipt 前实现 resume；
- Product facade 前实现 MoteCodingTask；
- policy-bound command service 前实现 CommandEvaluator；
- sealed fixture/final snapshot 前实现 ChangedFilesEvaluator。

关键路径的每个节点必须先有契约测试，再接下游实现。否则下游会通过读取私有对象或添加临时字段自行补洞，最终反向固化错误上游 API。

## 33. 实现前必须关闭的边界问题

### P0：Artifact 单一契约

- 主设计删除 Eval 通用 ArtifactRef。
- 复用 `contracts.artifact.ArtifactRef`。
- 定义 EvalArtifactRecord。
- 修改 architecture rule，允许 domain 只 import contracts 批准清单。

### P0：Product headless facade

- 建立稳定 request/receipt/cancel/reconcile/close API。
- CLI 与 Eval 共用同一路径。
- API 不暴露 Role、EngineServices 或 runtime 私有对象。
- headless 人机交互策略显式化。

### P0：命令执行门面

- Product 提供完整 permission/sandbox command service。
- ask 在 headless 下默认 deny。
- 命令进程组与 attempt scope 关联。
- 未完成前不实现 CommandEvaluator。

### P0：Eval durable core

- identity、codec、metric、provenance；
- state machine、event envelope、reducer；
- journal committer、replay 和 migration；
- CaseExecutor port 和 execution receipt；
- fresh attempt workspace；
- recovery/reconcile。

## 34. 推荐实施顺序

```text
Step 1  修订主设计的 Artifact、adapter 命名、所有权树和依赖白名单
Step 2  冻结 Eval identity/codec/metric/provenance 与 Product request/receipt 边界
Step 3  建立 Product headless Coding Application facade
Step 4  建立 Product PolicyBoundCommandService
Step 5  实现状态转移表、event envelope、纯 reducer 和 journal ports
Step 6  实现 durable committer、replay、migration 与 projection
Step 7  实现 fixture snapshot、WorkspaceManager、CaseExecutor port 和 InProcess 实现
Step 8  用纯函数 vertical slice 验证 ExperimentRunner 与 durable recovery
Step 9  实现 LocalProcessCaseExecutor、lease、cancel 和 reconcile
Step 10 接入 MoteCodingTask 与无命令确定性 evaluators
Step 11 实现 report、compare、verify 与基础 CLI
Step 12 接入 CommandEvaluator
```

该顺序确保 Eval 不会先依赖 CLI 私有装配、不重复 Artifact 契约，也不会在命令权限服务就绪前建立安全旁路。

## 35. 分阶段实施闸门

为避免“所有接口一起设计、所有实现一起落地”的大爆炸，建议采用四个不可越级的 gate：

### Gate A：契约闭合

- Artifact 单一契约已确定；
- identity、codec、metric、provenance 和 Product receipt 已版本化；
- adapter 命名和 import 白名单已有 architecture test；
- ownership tree 与状态转移表完成评审。

### Gate B：Durable core 闭合

- journal commit/replay/migration/reducer 同源；
- torn tail、重复事件、sequence conflict、未知 schema 测试通过；
- retry/resume/restart/repair 有独立 application commands；
- 不接 Mote 也能用纯函数 case 完成崩溃恢复闭环。

### Gate C：隔离执行闭合

- LocalProcessCaseExecutor 支持 lease、heartbeat、cancel、reconcile；
- fresh workspace、process group、artifact publication 和 cleanup receipt 可验证；
- coordinator/worker 在每个 phase boundary 的进程级崩溃测试通过。

### Gate D：Mote 适配闭合

- CLI 与 Eval 使用同一 Product headless facade；
- Coding attempt 不泄漏 Agent、Terminal/Python、background task 或子进程；
- headless ask fail closed；
- CommandEvaluator 只使用 Product policy-bound service；
- report/compare 能根据 provenance 拒绝不兼容实验。

只有 Gate A/B 完成后才适合大规模实现 runner；只有 Gate C 完成后才可宣称 Coding Eval 支持强超时和崩溃恢复。

## 36. 验收判断

当前可以判定：

- Mote Agent runtime 基础足够支持未来 Eval；
- Product composition 已有正确雏形；
- session、artifact 和 permission 领域存在可复用能力；
- Eval 自身实现完成度为零；
- Product 面向 Eval 的稳定公开门面尚未闭合；
- 主设计的 Artifact 边界仍需修订。

只有以下条件满足后，才可以认为 Mote 已经跨过“单 Agent 执行”边界，进入“可恢复实验执行”阶段：

1. Product headless facade 成为 CLI 与 Eval 的共同正式入口。
2. Eval journal/reducer 可以独立恢复 experiment 状态。
3. LocalProcessCaseExecutor 能对 attempt 实施取消、超时、对账和资源回收。
4. 所有 durable value 均经过 codec。
5. Eval artifact 复用唯一的 Contracts ArtifactRef。
6. CommandEvaluator 不绕过 Product 权限和 sandbox。
7. runner 可以从 journal reconcile 非终态 execution lease，而不会盲目重跑。

最终边界定义：当前实现止于“可靠运行单个 Agent”；`mote.eval` 要建设的是其上的“可恢复、可复现、可比较的实验控制平面”。
