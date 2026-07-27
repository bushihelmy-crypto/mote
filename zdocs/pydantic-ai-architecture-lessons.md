# Mote 吸收 Pydantic AI 架构优势的演进建议

## 1. 目标与基本判断

Mote 不应照搬 Pydantic AI 的 Agent Graph，也不应削弱现有的多 Agent、持久化、权限、调度和 Environment 能力。最值得吸收的是 Pydantic AI 在以下方面的优势：

- 小而稳定的公共 API；
- 贯穿依赖、工具和输出的类型契约；
- 正交、可组合的扩展点；
- 轻量核心包与可选集成边界；
- 显式、可观察的执行状态转换。

目标应当是：**保留 Mote 的 Runtime 内核，在外层提供像 Pydantic AI 一样小、强类型、稳定、可组合的开发者接口。**

### 1.1 十年架构与零债务约束

本文所有设计以至少十年演进周期为目标，不以最小改动、短期兼容或降低本次重构成本为优先级。允许大规模调整内部模块、类型、持久化格式和装配方式，但最终架构必须完成一次性收敛。

“零债务”采用可验证的工程定义，而不是口号：

- 零重复事实源：状态、历史、effect settlement 和 output acceptance 各自只有一个权威存储；
- 零平行执行内核：最终只保留一个默认 Agent 执行模型；
- 零永久兼容层：迁移 adapter 必须有删除版本和删除条件；
- 零新旧 API 长期并存：公共 API 一次收敛，内部旧入口删除而非无限 deprecated；
- 零隐式生命周期：资源获取、运行、提交、取消、恢复和释放都有显式协议；
- 零不可验证分层：依赖方向、Graph 合法转移和持久化不变量由自动化检查保证；
- 零恢复歧义：每个 crash point 都有唯一、可测试的恢复结论；
- 零静默降级：缺失 capability、非法状态和不支持的恢复必须显式失败；
- 零无界增长：history、journal、event、cache、background state 都有 retention/compaction 策略；
- 零实现泄漏：公共 API 不暴露 ComponentGraph、journal、provider wire format 或具体 Graph node。

高可用的最低设计目标包括：

- 进程崩溃后可确定性恢复；
- 外部副作用默认采用 at-most-once intent 与显式 reconciliation，不宣称无法保证的 exactly-once；
- journal、effect ledger、session history 和 committed output 的写入顺序有形式化不变量；
- cancellation、timeout、进程丢失和 worker lease 过期具有不同语义；
- 恢复过程可重复执行且自身幂等；
- 单节点故障不会污染后续 run；
- 所有外部资源都有结构化生命周期和 bounded cleanup；
- 关键恢复路径具备 fault injection、模型检查或属性测试。

优雅设计的判断标准是概念数量最少且边界最强，而不是文件或类型数量最少。允许为了消除长期歧义进行大改，但禁止通过增加第二套 registry、第二套 event、第二套 state 或第二套 runner 来规避迁移。

## 2. 优先级

| 优先级 | 改进 | 主要收益 |
| --- | --- | --- |
| P0 | 建立小而稳定的公共 API | 降低学习成本，隔离内部重构 |
| P0 | 将依赖拆为 core 与 extras | 提高可嵌入性和安装可靠性 |
| P0 | 强化泛型依赖与输出契约 | 减少运行时错误 |
| P1 | 统一 Tool、Toolset、Capability 模型 | 降低扩展点重叠和装配成本 |
| P1 | 提供轻量单 Agent 执行路径 | 避免普通用户理解完整 Runtime |
| P1 | 暴露稳定的 typed run event stream | 改善 UI、网关、观测和组合 |
| P2 | 引入局部 typed execution state machine | 提高流程可观察性和可恢复性 |
| P2 | 建立公共 API 兼容策略 | 支持第三方生态长期发展 |

## 3. 建立三层 API

当前内部架构中的 `Role`、`RoleSchema`、`RoleState`、`RoleComponents`、Router、ContextManager、Environment、Flow、Executor、Session 和 EventBus 不应全部成为普通使用者必须理解的公共模型。

建议形成三层 API：

```text
Level 1: Agent
  面向大多数单 Agent 使用者

Level 2: Runtime / Environment
  面向多 Agent、调度、持久化和长期运行

Level 3: Role / Components / Flow
  面向框架内部和高级扩展
```

新增薄 facade：

```python
from mote import Agent

agent = Agent(
    model="openai:gpt-5",
    instructions="You are a coding assistant.",
    deps_type=ProjectServices,
    output_type=ReviewResult,
)

result = await agent.run(
    "Review this repository",
    deps=services,
)
```

内部仍可构造 Role、ComponentGraph、Router 和 AgentFlowEngine，但这些不应泄漏到基础调用路径。核心原则是：

> `Agent` 是稳定产品接口，`Role` 是内部运行时实体。

## 4. 引入端到端泛型契约

让依赖和最终输出成为 Agent 类型的一部分：

```python
DepsT = TypeVar("DepsT")
OutputT = TypeVar("OutputT")


class Agent(Generic[DepsT, OutputT]):
    async def run(
        self,
        prompt: str,
        *,
        deps: DepsT,
    ) -> RunResult[OutputT]:
        ...
```

统一运行上下文：

```python
@dataclass(frozen=True)
class RunContext(Generic[DepsT]):
    deps: DepsT
    session_id: str
    run_id: str
    usage: Usage
    cancellation: CancellationToken
```

结构化输出：

```python
class ReviewResult(BaseModel):
    summary: str
    findings: list[Finding]
    score: float
```

Mote 不应因此取消工具的最小权限注入。完整的应用依赖容器不应直接交给每个工具，而应通过窄接口表达权限：

```python
@dataclass(frozen=True)
class ToolDeps:
    repository: RepositoryReader
    approval: ApprovalPort
```

即：吸收 Pydantic AI 的泛型上下文，同时保留 Mote 的 capability 白名单和最小权限边界。

## 5. 拆分轻量核心包

Mote 的组件在运行时可以 opt-in，但安装依赖尚未完全贯彻这一边界。建议拆分为：

```text
mote-core
  基础事件、上下文、flow、session 接口
  仅保留 pydantic、anyio、httpx、tenacity 等核心依赖

mote
  默认 facade，依赖 mote-core

mote[openai]
mote[anthropic]
mote[mcp]
mote[cli]
mote[browser]
mote[python]
mote[documents]
mote[observability]
mote[local-routing]
mote[durable-temporal]
mote[all]
```

优先移出核心依赖：

- Playwright；
- Jupyter；
- sentence-transformers、LightGBM、ONNX Runtime；
- Office/PDF 处理库；
- Langfuse、Sentry；
- Textual；
- 非默认 provider SDK。

Provider 和集成包只实现 `mote-core` 定义的 Protocol，核心包不得反向 import 具体 provider。

## 6. 收敛扩展模型

当前扩展机制包括 Protocol、ComponentSpec、工具和 provider registry、event subscriber、hook、context source、output engine、completion policy 和 Role subclass。建议对外收敛为五类概念：

```text
Model       模型请求协议
Toolset     模型可调用能力的集合
Capability  改变运行行为的横切能力
Policy      单个决策点的可替换算法
Observer    不改变执行语义的观察者
```

建议映射：

| 现有概念 | 对外收敛方向 |
| --- | --- |
| `BaseTool` | Tool |
| 工具注册、MCP、动态工具 | Toolset |
| hook、部分 control subscriber | Capability |
| completion、router、permission 策略 | Policy |
| tracing、日志、session recorder | Observer |
| ephemeral context source | ContextProvider 或 Capability |
| `ComponentSpec` | 保留为内部装配机制 |

`ComponentSpec` 不应成为第三方添加工具或 hook 时必须理解的主要入口。

## 7. 增加 Toolset 一等抽象

建议提供统一协议：

```python
class Toolset(Protocol[DepsT]):
    async def tools(
        self,
        ctx: RunContext[DepsT],
    ) -> Sequence[ToolDefinition]:
        ...

    async def call(
        self,
        ctx: RunContext[DepsT],
        call: ToolCall,
    ) -> ToolResult:
        ...
```

它应统一承载：

- 静态 Python 工具；
- MCP 工具；
- 按用户权限动态过滤的工具；
- 按模型能力过滤的工具；
- tool search；
- 远程或延迟加载工具；
- 多租户工具集合。

组合示例：

```python
tools = CombinedToolset(
    PythonToolset([...]),
    MCPToolset(server),
    PermissionFilteredToolset(admin_tools),
)
```

现有 `requires=(...)` 能力声明应保留在 Python Toolset 内部，继续作为最小权限边界。

## 8. 建立公开的 typed run event stream

内部 EventBus 不应直接作为 SDK 流式接口公开。建议新增稳定的事件联合类型：

```python
RunEvent = (
    RunStarted
    | ModelRequestStarted
    | ModelDelta
    | ToolCallStarted
    | ToolCallFinished
    | ApprovalRequested
    | ContextCompacted
    | AgentSpawned
    | AgentMessageReceived
    | OutputProduced
    | RunFinished
    | RunFailed
)
```

使用方式：

```python
async for event in agent.run_stream(prompt, deps=deps):
    match event:
        case ModelDelta(delta=text):
            print(text, end="")
        case ApprovalRequested() as request:
            await request.respond(True)
        case RunFinished(result=result):
            consume(result)
```

必须明确区分：

```text
Public RunEvent stream
  供应用、SDK、UI 和网关消费，稳定且支持背压

Control plane
  内部使用，可以改变执行结果

Observation EventBus
  日志、追踪、持久化等内部观察
```

这样内部事件可以继续演化，而不破坏公共 API。

## 9. 局部吸收显式执行图思想

Mote 已有 AgentFlowEngine、后台 DAG、ThinkJournal、OutputEngine 和 CompletionPolicy，不应再引入第二套通用 Agent Graph 或执行内核。

应吸收的是“显式状态转换”，将单次 turn 表达为：

```text
Observe
  -> PrepareContext
  -> ModelRequest
  -> ParseResponse
       -> ToolCalls -> ExecuteTools -> Observe
       -> CandidateOutput -> ValidateOutput
                                -> Retry -> ModelRequest
                                -> Finish
```

可以使用轻量 typed state machine：

```python
class TurnNode(Protocol):
    async def run(self, ctx: TurnContext) -> TurnTransition:
        ...
```

由此获得：

- 明确的 durable checkpoint 边界；
- 可观察的 retry 和 completion 路径；
- 更清晰的 cancellation 语义；
- 集中的 output validation；
- 可替换 Graph 定义，而不影响 Environment 或复制执行内核。

## 10. 分离五类状态

在现有 `RoleSchema` 与 `RoleState` 基础上进一步区分：

```text
AgentConfig       Agent 创建时确定的静态配置
RunOptions        单次调用覆盖项
RunState          一次执行内变化的状态
SessionState      跨 run 持久化状态
EnvironmentState  多 Agent 共享状态
```

示例：

```python
@dataclass(frozen=True)
class AgentConfig:
    model: Model
    toolsets: tuple[Toolset, ...]
    output_type: type
    default_policy: RunPolicy


@dataclass(frozen=True)
class RunOptions:
    model_settings: ModelSettings | None = None
    usage_limits: UsageLimits | None = None
    timeout: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass
class RunState:
    run_id: str
    usage: Usage
    history: list[Message]
    step: int
```

这可以防止 `RoleSchema` 随功能增长成为总配置对象。

## 11. 将输出契约提升为公共一等抽象

建议支持统一的输出声明：

```python
Agent(output_type=str)
Agent(output_type=ReviewResult)
Agent(output=PromptedOutput(ReviewResult))
Agent(output=NativeOutput(ReviewResult))
Agent(output=ToolOutput(ReviewResult))
```

所有路径返回统一结果：

```python
@dataclass
class RunResult(Generic[OutputT]):
    output: OutputT
    usage: Usage
    messages: Sequence[Message]
    run_id: str
    session_id: str
```

输出契约应成为 Agent 类型和公共 API 的组成部分，而不只是内部 OutputEngine 的配置。

## 12. 建立公共与内部 API 硬边界

建议逐步形成如下结构：

```text
mote/
  __init__.py       极少量稳定公共导出
  agent.py          Agent facade
  run.py            RunContext、RunResult、RunEvent
  tools.py          Tool、Toolset 公共协议
  models.py         Model 公共协议
  output.py         OutputSpec
  runtime/          Runtime、Environment 高级 API
  _internal/        ComponentGraph、wiring 等实现
```

顶层建议只导出：

```python
__all__ = [
    "Agent",
    "RunContext",
    "RunResult",
    "RunEvent",
    "Tool",
    "Toolset",
    "Model",
    "OutputSpec",
    "Runtime",
]
```

以下类型原则上不应成为稳定公共 API：

- `RoleComponents`；
- `ComponentGraph`；
- builder context；
- 内部 subscriber；
- session recorder 实现；
- 具体 router 实现。

## 13. 不应照搬的部分

### 13.1 不弱化 Runtime 层

Environment、session replay、mailbox、scheduler、durable execution 和权限边界是 Mote 的核心优势，不应为了表面 API 简单而压回 Agent 类。

### 13.2 不建立第二套通用图系统

否则会出现两套节点协议、durable 语义、事件模型、cancellation 和状态持久化机制。只吸收显式状态转换思想。

### 13.3 不把完整依赖容器交给工具

Coding Agent 属于安全敏感系统，应继续坚持 capability 白名单和窄接口注入。

### 13.4 不让 Capability 成为万能中间件

Capability 如果能任意修改 history、tools、events、output 和 model request，将产生难以预测的组合顺序。应继续保留明确的控制事件类型和决策点。

## 14. Agent Graph 作为统一 Flow 执行模型

Mote 的目标态不保留 Loop 抽象，以唯一的 `AgentFlowEngine` 执行类型化 Graph；ReAct、plan/execute、review/refine 的差异由 Graph 定义表达，而不是由多套 Engine 复制领域逻辑：

```text
Role
  -> AgentFlowEngine
       |- ReActGraph
       |- PlanExecuteGraph
       `- ReviewRefineGraph
```

这样保留两个重要边界：

- Role 只装配并启动 `AgentFlowEngine`，不理解具体 Graph 拓扑；
- Environment、mailbox、scheduler、residency 继续位于单 Agent 执行图之上。

迁移期曾用旧 `ReActLoop` 作为一次性行为 oracle；当前它与 `BaseLoop`、旧 durable 分支及迁移 adapter 均已删除，`AgentFlowEngine` 是唯一执行内核。Engine 是内部实现，公共 API 只承诺 Agent/Runtime 行为和 `flow/events.py` 的 typed `RunEvent`，不承诺具体 Graph node。当前 ReAct 与 Review/refine 两种生产拓扑已复用同一组公共节点、`FlowServices` 和 `DurableFlowRunner`。

### 14.1 AgentFlowEngine 的职责

`AgentFlowEngine` 负责：

- 驱动一次 Agent run 的节点转移；
- 将节点事件投影为稳定的 public `RunEvent`；
- 调用统一 durable transition runner；
- 在节点边界执行 cancellation、checkpoint 和恢复协议；
- 最终返回统一的 `FlowResult`。

`AgentFlowEngine` 不负责：

- 构造 Role 组件；
- 管理 Agent residency；
- 调度多个 Agent；
- 实现具体 provider；
- 直接执行工具副作用；
- 把 Environment 行为建模为节点。

建议接口：

```python
class AgentFlowEngine:
    def __init__(
        self,
        *,
        graph: AgentGraph,
        runner: DurableGraphRunner,
        context_provider: BaseContextProvider,
    ) -> None:
        ...

    async def run(self) -> FlowResult | None:
        ...
```

### 14.2 建议的 Agent Graph

第一版图应保持粗粒度和稳定，不把每个 helper 都变成节点：

```text
Start
  -> RestoreTerminalOutput
  -> Observe
       |- no input -------------------------------> End(no result)
       `- input
            -> BudgetGate
                 |- hard stop --------------------> Finish
                 `- proceed
                      -> Think
                      -> InterpretTurn
                           |- tool actions --------> ExecuteActions
                           |                           -> RecordResults
                           |                           -> Observe
                           |
                           |- output candidate ----> ValidateOutput
                           |                           |- reject/correct -> Think
                           |                           `- accept --------> CommitOutput -> Finish
                           |
                           `- inactive/no todo
                                |- background pending -> WaitBackground -> Observe
                                `- none --------------> Finish
```

建议节点职责如下：

| 节点 | 职责 | 副作用分类 |
| --- | --- | --- |
| `RestoreTerminalOutputNode` | 恢复已接受但未完成发布的输出 | ledgered |
| `ObserveNode` | mailbox/buffer 过滤并写入 memory | ledgered |
| `BudgetGateNode` | 预算判断，不调用模型 | pure |
| `ThinkNode` | 准备上下文并调用模型 | replayable |
| `InterpretTurnNode` | 将 channel 输出解释为语义 turn | pure |
| `ExecuteActionsNode` | 执行工具批次 | mixed/external |
| `RecordResultsNode` | 保证 tool-call/result 配对并持久化 | ledgered |
| `ValidateOutputNode` | 解码、校验和产生纠正反馈 | pure 或 replayable |
| `CommitOutputNode` | 提交已接受输出 | ledgered |
| `WaitBackgroundNode` | 等待后台任务完成 | waitable |
| `FinishNode` | 构造统一 `FlowResult` | pure |

`InterpretTurnNode` 应继续复用现有 `CommandChannel`、`CompletionPolicy` 和语义 `ModelTurn`，不能让 provider wire format 泄漏到 Graph。

### 14.3 Typed transition

节点之间不应依赖字符串名称或任意字典，而应通过封闭的类型联合表达合法转移：

```python
@dataclass(frozen=True)
class ThinkTransition:
    pass


@dataclass(frozen=True)
class ExecuteActionsTransition:
    turn: ModelTurn


@dataclass(frozen=True)
class ValidateOutputTransition:
    candidate: OutputCandidate


@dataclass(frozen=True)
class WaitBackgroundTransition:
    pass


@dataclass(frozen=True)
class FinishTransition:
    result: FlowResult


AgentTransition = (
    ThinkTransition
    | ExecuteActionsTransition
    | ValidateOutputTransition
    | WaitBackgroundTransition
    | FinishTransition
)
```

每个节点的返回类型应只包含该节点允许产生的后继。例如：

```python
class InterpretTurnNode:
    async def run(
        self,
        ctx: GraphRunContext,
    ) -> ExecuteActionsTransition | ValidateOutputTransition | FinishTransition:
        ...
```

这样可以在类型检查和 graph validation 阶段发现非法转移。

### 14.4 状态边界

不能把 Mote 所有状态塞进一个 `GraphState`。必须保持以下边界：

```text
FlowState
  单次 flow 执行的当前位置、turn、候选输出、使用量引用

SessionState
  跨 run 的历史、压缩检查点、accepted output

EnvironmentState
  Agent residency、mailbox、scheduler、多 Agent 共享状态

EffectLedger
  外部工具副作用的开始、结算与 unknown-after-crash 状态

NodeLocalState
  streaming 进度等不需要跨节点共享的瞬时状态
```

建议最小 `FlowState`：

```python
@dataclass
class FlowState:
    run_id: str
    turn_index: int = 0
    latest_observed_message: Message | None = None
    current_turn: ModelTurn | None = None
    output_candidate: OutputCandidate | None = None
    presentation: Message | None = None
    committed_output: CommittedOutput[Any] | None = None
```

消息历史仍由 MessageStore/SessionLog 持有，工具结算仍由 EffectLedger 持有，不能为了序列化方便复制进 GraphState，制造多个事实来源。

### 14.5 Effect-aware durable runner

Agent Graph 能否长期可靠运行的关键不是 GraphBuilder，而是唯一的 `DurableFlowRunner`。它应理解节点的副作用分类：

```python
class EffectKind(Enum):
    PURE = "pure"
    REPLAYABLE = "replayable"
    LEDGERED = "ledgered"
    EXTERNAL = "external"
    WAITABLE = "waitable"
```

节点声明元数据：

```python
class ExecuteActionsNode:
    effect_kind = EffectKind.EXTERNAL
```

Runner 负责统一处理：

```text
before node
  -> 检查恢复记录
  -> 根据 effect kind 决定重放、恢复或 reconcile
  -> 写入 node-started/checkpoint

run node
  -> 发布 typed node events
  -> 执行节点

after node
  -> 持久化 transition
  -> 结算 effect
  -> 标记 node-completed

cancel/failure
  -> 区分 abandoned、failed、unknown-after-crash
  -> 修复协议配对
  -> 保留或清理恢复记录
```

当前实现将该规则固化为 `NodeAttempt`：每次 Node 调用都必须经过
`DurableFlowRunner` 的唯一边界，且恢复指令由 `EffectKind` 封闭映射：

| EffectKind | RecoveryDirective | 约束 |
| --- | --- | --- |
| PURE | RESTART | 可从安全边界重新计算 |
| REPLAYABLE | REINSTATE | 优先恢复已持久化结果 |
| LEDGERED | RECONCILE | 先查询领域台账，禁止盲重跑 |
| EXTERNAL | RECONCILE | 状态不明必须显式对账，runner 不自动重试 |
| WAITABLE | RESUME_WAIT | 从持久化 deadline/等待条件继续 |

Runner 只裁定恢复类别和发布节点生命周期；ThinkJournal、EffectLedger、
OutputEngine 仍分别拥有结果恢复、外部效果结算与输出提交，避免出现第二套事实来源。

建议协议：

```python
class DurableFlowRunner:
    async def run_node(
        self,
        node: AgentNode[StateT, TransitionT],
        ctx: GraphRunContext[StateT],
    ) -> TransitionT:
        ...
```

现有 `ThinkJournal`、EffectLedger、OutputEngine 和 SessionLog 应被该 runner 组合使用，而不是重新实现一套平行持久化系统。节点生命周期只协调恢复策略；工具副作用的具体结算仍由 ToolExecutor/EffectLedger 唯一负责，不能宣称通用 exactly-once。崩溃后状态不明的外部效果必须显式 reconcile。

### 14.6 工具节点不能简单重跑

`ExecuteActionsNode` 必须保留 `ActionExecutionService` 已建立的精确协议：

```text
完成并记忆 think result
  -> 判断工具是否需要 ledger
  -> 先写 assistant tool_call 并 flush
  -> 执行工具
  -> 为每个 call 写入 tool_result
  -> 中断时为未结算 call 写入 INTERRUPTED
  -> 崩溃恢复时通过 EffectLedger reconcile
  -> 结果持久化后才关闭 think 恢复窗口
```

不能使用通用的“节点失败后重试”规则处理外部工具。Runner 至少要区分：

- 尚未开始；
- 已开始但未产生外部副作用；
- 副作用已结算；
- 副作用状态未知；
- result 已记录；
- call/result pairing 尚待修复。

`ExecuteActionsNode` 只负责拓扑跳转，直接复用 `ActionExecutionService`；不能把事务顺序重新散回 Node 或 Runner。

### 14.7 Streaming 与事件

Graph 的内部节点事件不能成为第三套公共事件系统。事件关系应固定为：

```text
Node/Transition internal events
  -> GraphRunEventProjector
       -> public RunEvent stream
       -> Observation EventBus

Control EventBus
  -> 仍用于可以改变执行行为的控制决策
```

上层 Agent facade 可公开：

```python
async def run_stream(...) -> AsyncIterator[RunEvent]:
    ...
```

但以下内部事件不应直接公开：

- ComponentGraph 构建事件；
- journal 内部状态；
- EffectLedger 存储格式；
-具体 node class 路径；
- provider/channel 私有事件。

用户 interjection 应继续进入 mailbox/message buffer，由 `ObserveNode` 在安全边界消费，而不是随时突变 GraphState。后台任务完成也应通过消息唤醒 `WaitBackgroundNode`，不应从外部直接跳转 graph 当前节点。

### 14.8 唯一 Engine 与 Graph 的共享服务

所有 Graph 必须复用同一个事实来源：

```text
ContextProvider
ThinkEngine
CommandChannel
ToolExecutor
CompletionPolicy
OutputEngine
MessageStore
EffectLedger
SessionLog
TurnContextBus
```

禁止为新 Graph 建立第二套：

- message history；
-工具 registry；
-输出 validator；
-durable journal；
-EventBus；
-provider adapter。

Graph 只定义控制流表示和 transition，不替换执行内核或已有领域服务。

### 14.9 装配方式

`RoleComponents.make_flow_engine()` 只构造唯一的 `AgentFlowEngine`。Engine 装配一个无副作用 builder 生成的 Graph；ReAct 是默认 Graph 定义，不是 Engine 子类，也不存在 `loop_kind` 注册表。Node 只接收不可变 `FlowServices` 和瞬时 `FlowState`，不得反向持有 Role 或 Engine。

### 14.10 实施顺序

#### 阶段 A：抽取共享领域服务（已完成）

1. 从 `_step_act` 抽取 `ToolBatchExecutor`；
2. 抽取 `TurnRecorder`，统一 call/result pairing；
3. 抽取 `EffectCheckpointService`；
4. 将 durable turn-boundary 规则提升为可复用协议；
5. 用契约测试保证原有行为不变。

#### 阶段 B：实现唯一 Graph 执行内核（已完成）

1. 实现 typed `FlowState` 和 transition union；
2. 实现 `Observe/Think/Interpret/Execute/Validate/Commit/Finish` 节点；
3. 由 `AgentFlowEngine` 和 `DurableFlowRunner` 执行；
4. 运行长期行为契约测试；
5. 确认 message history 和输出完全等价。

#### 阶段 C：接入 durable runner（已完成）

1. 接入 think reinstate；
2. 接入 external-effect checkpoint；
3. 接入 cancellation cleanup；
4. 接入 terminal output restore；
5. 接入 background wait/resume；
6. 完成 crash-point fault injection 测试。

#### 阶段 D：切换并删除旧内核

AgentFlowEngine 满足以下发布门槛后直接切换为唯一内核：

- 没有复制现有领域服务；
- durable 规则集中在 runner、checkpoint 与领域服务；
- 至少两种执行策略能够复用节点或子图；
- graph event 可以稳定投影为 public RunEvent；
- 性能和内存没有不可接受的回退；
- 所有 crash point 都有唯一恢复结果；
- ReActGraph、plan/execute 或 review/refine 至少有两种图能够复用公共节点和 runner；
- 性能和资源上限满足明确 SLO。

切换版本必须同时删除：

- 旧 `ReActLoop` 实现；
- 仅由旧 Loop 使用的 durable helper；
- 新旧 Loop 选择开关；
- 双写、双读和结果对比代码；
- 迁移期别名与兼容 adapter。

门槛未满足时应继续完成 Graph 内核，而不是把双轨状态固化为长期产品设计。

### 14.11 行为等价性测试矩阵

迁移验证使用旧实现结果作为一次性行为 oracle，并将下列场景固化为与具体 Engine/Graph 无关的长期契约测试：

| 场景 | 必须验证的结果 |
| --- | --- |
| 普通文本完成 | 相同 presentation、history 和 committed output |
| 单工具调用 | call/result 正确配对 |
| 多工具顺序执行 | 顺序、首错跳过语义一致 |
| 工具审批拒绝 | flow 终止和结果记录一致 |
| 工具返回媒体 | image/PDF/retention 元数据一致 |
| 输出校验失败 | correction feedback 与重试次数一致 |
| 输出校验耗尽 | 抛出相同领域异常 |
| LLM think 中断 | 不错误恢复已取消计划 |
| 外部工具中断 | 未结算 call 获得 INTERRUPTED result |
| 外部工具中崩溃 | EffectLedger 能 reconcile，不重复副作用 |
| 输出接受后崩溃 | 恢复后只 commit/publish，不重新校验 |
| 用户 interjection | 在下一安全观察边界进入历史 |
| 后台任务完成 | 能唤醒并重新 observe |
| 预算硬停止 | 在模型请求前终止 |
| XML/native channel | 语义行为一致，wire history 各自正确 |

应增加 fault injection 点：

```text
after-think-before-record
after-call-record-before-effect
after-effect-before-result
after-result-before-journal-reap
after-output-accept-before-history
after-output-history-before-commit
```

### 14.12 成功标准

Agent Graph 融合的成功不以“流程画成图”为标准，而以以下结果衡量：

- 新增 review/refine 或 plan/execute 流程时能复用既有节点；
- 合法转移由类型系统和 graph validator 约束；
- durable、effect 和 cancellation 规则集中在 runner，而非散落各节点；
- 默认 ReAct 行为没有退化；
- GraphState 没有成为新的 God Object；
- Environment 和单 Agent Graph 的层次保持分离；
- public RunEvent 不暴露 graph 内部实现；
- ReActGraph、PlanExecuteGraph 等图只改变拓扑，不复制 runner 和领域服务；
- 迁移完成后仓库中不存在第二套 Agent 执行内核。

最终推荐结构：

```text
Environment
  -> Role
       -> AgentFlowEngine
            |- Typed Agent Graph
            |    |- ReActGraph
            |    |- PlanExecuteGraph
            |    `- ReviewRefineGraph
            `- DurableFlowRunner
                 |- SessionLog / ThinkJournal
                 |- EffectLedger
                 `- OutputEngine
```

这一方案吸收 Agent Graph 的显式状态转移、可验证拓扑和节点复用能力，同时保留 Mote 在多 Agent Runtime、工具副作用、崩溃恢复和权限隔离方面的现有优势。

## 15. 整体演进路线图

### 第一阶段：改善外部体验，不改内核

1. 新增 `Agent[DepsT, OutputT]` facade；
2. 新增 `RunContext` 和 `RunResult`；
3. 提供统一结构化输出入口；
4. 划定顶层公共 API；
5. 提供最小单 Agent 示例和兼容测试。

### 第二阶段：收敛扩展点

1. 引入 Toolset；
2. 区分 Capability、Policy 和 Observer；
3. 建立 public typed event stream；
4. 将 ComponentSpec 明确降为内部机制；
5. 建立扩展点执行顺序与冲突规则。

### 第三阶段：物理模块化

1. 拆分 `mote-core`；
2. 将 provider 改为 extras；
3. 拆出 browser、documents、ML routing 和 observability；
4. 增加 import-boundary 自动检查；
5. 验证最小安装不加载可选模块。

### 第四阶段：显式执行状态机

1. 将 turn 拆成 typed transitions；
2. 对齐 durable checkpoint；
3. 统一 retry、validation 和 completion；
4. 让自定义 Graph 基于稳定 node/transition 协议扩展，共享唯一 Engine。

## 16. 验收标准

完成上述演进后，应满足：

- 新用户只理解 `Agent.run()` 即可完成基础使用；
- 高级用户可显式进入 Runtime/Environment 层；
- 工具、模型、输出和运行结果保持端到端类型安全；
- 添加普通工具不需要理解 ComponentGraph；
- 最小安装不包含浏览器、文档、ML 和 UI 重依赖；
- 公共事件流不泄漏内部 EventBus 类型；
- Role 和 ComponentGraph 可以重构而不破坏基础用户代码；
- durable、多 Agent、权限和 session replay 能力不因 facade 简化而退化；
- 分层依赖由自动化检查保证，而不只依赖文档约定。
