# Mote 泛型与类型安全体系改造需求

状态：待评审  
性质：架构级技术需求  
适用范围：`contracts <- kernel <- runtime <- orchestration <- product`  
目标周期：分阶段实施，不要求一次性大爆炸迁移

## 1. 背景

Mote 已经建立了若干正确的泛型主干：

- `Role[DepsT, OutputT]`、`AgentDependencies[DepsT, OutputT]`、`AgentWiring[DepsT, OutputT]` 表达 Agent 依赖与输出类型；
- `OutputContract[OutputT]`、`CommittedOutput[OutputT]`、`RunResult[OutputT]` 表达结构化输出；
- `AgentGraph[StateT, ResultT]`、`ExecutionState[OutputT]`、`ExecutionResult[OutputT]` 表达执行图状态与结果；
- `RunContext[DepsT]`、`ToolContext[ToolDepsT]` 表达 Agent 依赖到工具最小权限依赖的投影；
- `XmlToolset[AgentDepsT]`、`NativeToolset[AgentDepsT]` 已阻止 XML/Native 工具集的静态误组合；
- `EventEnvelope[PayloadT]` 表达事件载荷类型。

但这些泛型目前没有在关键链路中端到端闭合。部分中间组件使用 `Any`、无参 `Callable`、无类型 `Protocol` 或宽泛容器，使上游已经获得的类型信息在进入执行引擎、工具生命周期、工厂、事件投影后丢失。其结果是：

1. `Role[DepsT, OutputT]` 对外承诺了类型化输出，但执行引擎内部退化为 `ExecutionState[Any]` 和 `ExecutionResult[Any]`；
2. `AgentDependencies[DepsT, OutputT]` 持有的工具集退化成 `AnyToolset`，无法静态证明工具集依赖与 Agent 依赖兼容；
3. 工具定义只泛化 capability，却没有把 `RunContext[AgentDepsT]` 与 approval/lifecycle 回调完整关联；
4. Agent 工厂通过 `type[AgentT] + **kwargs: Any` 构造，无法检查具体 Agent 构造参数与 wiring/output 的一致性；
5. 遥测、展示 consumer/projector 等事件通道以 `Any` 传递，封闭事件联合在下游失效；
6. 类型检查仍为 `basic`，且当前全库检查混有依赖环境与第三方 SDK 噪声，无法作为可靠的架构门禁。

这不是要求“把所有 `Any` 替换成泛型”。本需求只处理能够表达稳定类型关系、且错误会跨组件传播的边界。JSON、外部 SDK、反序列化、动态插件发现等真正动态的系统边界仍允许使用受控的 `Any` 或 `object`，但必须在边界处校验并尽快收窄。

## 2. 总目标

建立一套可以长期演进的类型架构，使 Mote 在不牺牲动态扩展能力的前提下满足：

1. Agent 的依赖类型和输出类型从 Product 组合根贯穿到 Runtime、Kernel、输出提交与调用方，不在中途退化为 `Any`；
2. 工具只能声明并获得其最小依赖投影，类型系统能够检查 projector、tool function、approval policy 和生命周期回调之间的一致性；
3. 图状态、图节点、操作服务和图结果共享同一组类型参数，错误组合在静态检查阶段失败；
4. Agent 工厂、catalog、engine 等构造边界具备可检查的结构化接口，不依赖裸 `**kwargs: Any` 维持核心正确性；
5. 内部事件通道保留封闭事件联合或明确的输入/输出类型，不因通用 consumer/projector 抹除类型；
6. 类型检查采用分层、渐进、可执行的质量门禁；新增核心代码不得扩大 `Any` 和 unknown 的债务；
7. 泛型设计保持少而稳定，以表达真实的不变量为准，不引入 Java 式类型层级或无收益的高阶抽象。

## 3. 非目标

本需求不包括：

- 为每一个 DTO、配置类或普通容器增加类型参数；
- 消灭所有 `Any`；
- 用泛型模拟运行时权限检查、schema validation、协议协商或持久化版本兼容；
- 改变 `contracts <- kernel <- runtime <- orchestration <- product` 的依赖方向；
- 将 `Role` 改回继承型巨型基类；
- 将静态类型当成运行时输入验证的替代品；
- 为兼容旧类型别名保留长期 re-export、双接口或废弃层；
- 仅为了通过类型检查而增加无语义的 `cast`、`type: ignore` 或局部动态 import。

## 4. 设计原则

### 4.1 只泛化真实关联关系

只有当两个或多个位置必须保持同一种类型时才引入 `TypeVar`。例如：

- `OutputContract[T] -> OutputEngine[T] -> ExecutionState[T] -> ExecutionResult[T] -> RunResult[T]`；
- `AgentDependencies[D, O] -> Role[D, O] -> RunContext[D]`；
- `RunContext[D] --project--> ToolContext[TD] -> tool(ctx: ToolContext[TD], ...)`；
- `Projector[E, V] : E -> Sequence[V]`。

仅表示“可能是任意值”的字段不应机械泛型化，应优先选择 `object`、封闭联合、`JsonValue` 或经过校验的领域类型。

### 4.2 在边界解析动态性，在核心保留静态性

外部 JSON、Pydantic、SDK 对象、MCP/ACP/gRPC 载荷可在 adapter 边界保持动态；进入领域核心前必须完成 decode/validate/narrow。核心层不应长期携带 `dict[str, Any]` 或第三方 SDK 类型。

### 4.3 Protocol 表达能力，泛型表达关联

- 使用窄 `Protocol` 描述组件需要的行为；
- 使用泛型参数表达输入与输出之间的关联；
- 不用继承层级表达纯结构能力；
- 不建立 generic `utils` 或新的公共杂物包；跨层端口必须位于 `contracts/ports/`。

### 4.4 方差必须由读写语义决定

- 只生产 `T` 的接口可协变；
- 只消费 `T` 的接口可逆变；
- 同时读写 `T` 的接口保持不变；
- 不为消除检查器报错而随意声明方差。

### 4.5 不让持久化格式依赖 Python 泛型实现

泛型只服务静态检查。rollout、事件 envelope、checkpoint、manifest 的 wire schema 必须继续通过显式 `type/schema_version/contract_id` 等字段定义，禁止依赖 `__orig_class__`、运行时 TypeVar 或 Python 特定类型元数据。

### 4.6 一个动态逃生口必须有一个收窄点

保留 `Any` 时必须满足至少一项：

- 外部库缺少可靠类型；
- 数据尚未 decode；
- 插件/注册表的异构存储确实无法在容器层保留具体类型；
- 类型信息在该边界客观不可恢复。

并且应在最近的边界使用 runtime validation、`TypeGuard`、解析器或领域构造器恢复具体类型。

## 5. 需求范围与优先级

### Gate 0：冻结语言与类型检查执行契约

#### R0.1 统一 Python 支持政策

本需求正式采用以下版本政策：

- 最低支持版本立即统一为 Python 3.11；
- 项目元数据、classifiers、pyright `pythonVersion`、本地开发环境与 CI 不得声明互相冲突的最低版本；
- CI 覆盖“最低支持版 + 当前主支持版 + 最新稳定版”；三者重合时允许去重；
- 至少每两个 Python 发布周期复审一次最低版本；
- 后续最低版本变更必须通过独立 ADR，不在普通功能变更中顺带调整。

Gate 0 必须先修正当前 `requires-python >=3.9`、pyright 3.11 语义、CI 包含 3.10、源码已使用 `StrEnum` 的不一致。完成前不得启动后续泛型实现。

#### R0.2 固定检查环境

pyright 是唯一权威静态类型检查器，不同时维护 mypy。其版本必须精确锁定，并通过受控依赖升级单独更新。

提供可重复执行的类型检查环境，安装项目本身、项目声明的类型检查依赖及被检查模块所需依赖。缺失第三方包不得与产品类型错误混在同一基线中。

#### R0.3 建立机器可执行的类型岛

不直接对全库切换 `strict`。必须建立独立配置与固定命令，至少包括：

```text
typecheck/
  pyright.contracts.json
  pyright.execution.json
  pyright.runtime-tools.json
  pyright.orchestration.json
  pyright.presentation.json
  cases/
    pass/
    fail/
  case-expectations.json
  migration-diagnostics.json
  dynamic-boundaries.json
```

对应检查岛为：

1. `contracts` 核心领域模型与 ports；
2. `kernel/execution` 与 `kernel/output`；
3. `runtime/agent` 与 `runtime/tools`；
4. `orchestration/agents` 与 background tasks；
5. `product` 组合根和 presentation spine。

每份配置必须明确 `include`、`exclude`、execution environment、Python 版本和全部覆盖规则。岛引用尚未治理代码时，只允许通过已登记的窄 Protocol、typed adapter 或动态边界进入；不得因被依赖模块未治理而关闭本岛的 unknown 规则。

每个已收口检查岛必须启用或等效实现：

- 禁止 unknown 参数、返回值和成员类型；
- 禁止缺失泛型参数；
- public API 中禁止 unknown；
- 禁止 `Any` 跨越该岛声明的模块边界；
- 禁止未标注函数；
- `type: ignore` 必须带具体 rule，并有可审计理由。

CI 必须无条件运行全部类型岛的固定命令，不能依赖 changed-files 检查。pre-commit 可保留增量检查，但不构成权威门禁。

`cases/pass` 必须零诊断；`cases/fail` 必须通过 `case-expectations.json` 比对 case、预期 rule 与定位，证明错误组合确实失败。该文件是长期类型契约，不带 owner/expiry，也不计入迁移债务。测试不能依赖注释或人工阅读输出。

CI 必须使用两个语义独立的命令：

```text
verify_type_contract_cases
verify_migration_diagnostics
```

前者只验证 `cases/` 与 `case-expectations.json`；后者只验证产品源码与 `migration-diagnostics.json`。两者不得相互覆盖，产品源码不得放入 `cases/fail` 伪装成永久负例。

#### R0.4 建立可审计诊断棘轮

禁止用总错误数或显式 `Any` 裸计数作为主要门禁。未治理产品源码的临时例外必须逐条记录在 `migration-diagnostics.json`，且该文件不得覆盖 `cases/`。每项至少包含：

- `file`；
- `rule`；
- 稳定位置或诊断 fingerprint；
- justification；
- owner；
- expiry。

CI 对实际诊断与清单做精确比对：新增、漂移和过期例外均失败；已消失诊断必须删除。合并、重命名或 pyright 升级引起的 fingerprint 变化必须经所有者复核，不允许自动重建基线。

例外清单是迁移设施，不是永久基线。每个类型岛转为严格治理后必须删除该岛的全部历史例外。`dynamic-boundaries.json` 单独登记合理的动态擦除点、数据来源、校验位置、责任人和复审日期；不得用 `object`、别名或无约束 TypeVar 规避登记。

### Gate 1：批准四份类型 ADR

修改产品代码前必须批准四份短 ADR：

1. typed output 与稳定 run event 摘要；
2. Toolset 方差、definition 擦除和注册校验；
3. Runtime construction request 与 Product builder；
4. 异构 Telemetry core 与 typed edge。

本需求已经冻结 ADR 必须遵守的结论；ADR 负责记录详细类型签名、被否决方案、兼容与迁移测试，不得重新开放本需求已经裁决的方向。

其中 construction ADR 必须逐项描述 admission、residency/identity/nickname/path reservation、request 构造、builder 调用、cleanup 接管、provisioning、inert registration、supervision、commit 和逆序 rollback；不得只给出 catalog/builder 的静态签名而省略事务时序。

### P0：闭合 typed output 执行主链

#### R1.1 泛化执行引擎

`ExecutionEngine` 应以 `OutputT` 泛化，并保证：

```text
OutputContract[OutputT]
  -> OutputEngine[OutputT]
  -> OutputTransactionPort[OutputT]
  -> OutputOperation[OutputT]
  -> CommittedOutput[OutputT]
  -> ExecutionState[OutputT]
  -> AgentGraph[ExecutionState[OutputT], ExecutionResult[OutputT] | None]
  -> ExecutionEngine[OutputT].run()
  -> Role[DepsT, OutputT].run()
  -> RunResult[OutputT]
```

核心链路中不得再以 `ExecutionState[Any]`、`ExecutionResult[Any]` 或无类型 `output_engine` 作为默认签名。

#### R1.2 对执行中间态建模

`ExecutionState.turn: Any` 必须消除，并冻结为显式领域联合：

```python
@dataclass(frozen=True, slots=True)
class NoModelTurn:
    pass

@dataclass(frozen=True, slots=True)
class CandidateSelection:
    turn: ModelTurn
    candidate_index: int

ExecutionTurn = NoModelTurn | ModelTurn | CandidateSelection
```

`NoModelTurn` 统一表示“当前不存在待处理的 model turn”，具体执行阶段由 graph node 决定。候选索引必须在构造 `CandidateSelection` 时验证。不得再用 `None` 或 `(ModelTurn, index)` tuple 作为隐式状态协议，也不得用额外 TypeVar 把未知中间态简单包装成 `TurnT` 后由各节点任意解释。

#### R1.3 类型化 GraphAssemblyInputs

`GraphAssemblyInputs` 中的 `observation/inference/actions/outputs/context_provider/completion_policy/current_channel` 必须替换为窄类型。放置规则为：

- Kernel 内部单一生产实现使用具体 Kernel 类型；
- graph builder 确有多个实现且只依赖窄行为时，在 `kernel/execution/` 内定义 Protocol；
- 真正跨层注入的稳定能力才进入 `contracts/ports/`；
- 不得仅为了 mock 为每个 operation 建立一套镜像 Protocol。

`GraphAssemblyInputs` 可以对 `OutputT` 泛化，但不应泛化每个组件实现类型。

现有 `ExecutionTransactionPort` 不整体泛化：history、model turn、tool result 和 recovery 操作与 `OutputT` 无关。必须拆出窄的 typed output transaction port：

```python
class OutputTransactionPort(Protocol[OutputT]):
    async def stage_accepted_output(
        self,
        context: ExecutionOperationContext,
        output: AcceptedOutput[OutputT],
        history: HistoryProjection,
    ) -> MutationResult: ...

    async def commit_terminal_output(
        self,
        context: ExecutionOperationContext,
        staged_output_id: str,
    ) -> CommittedOutput[OutputT] | MutationResult: ...
```

Runtime 的具体 transaction 可以同时实现非泛型 `ExecutionTransactionPort` 与 `OutputTransactionPort[OutputT]`，但 Kernel 只依赖对应窄端口。typed output 主链不得出现 `AcceptedOutput[Any]`、`CommittedOutput[Any]` 或恢复类型的 cast。

现有 Contracts output evaluation port 也必须按 Kernel 的真实使用面扩充或拆分，至少覆盖 run ID、evaluate、staged/accepted/restore 读取能力、contract encoder 和 terminal output 状态。允许由 typed-output ADR 在 `OutputEvaluationPort[OutputT]`、`OutputRestorePort[OutputT]`、`OutputStateView[OutputT]` 与一个经证明仍足够窄的 `ExecutionOutputPort[OutputT]` 之间定案。Kernel 不得为获得这些能力直接 import Runtime 的具体 OutputEngine。

#### R1.4 Run event 不泛化，只报告稳定摘要

`RunEvent` 和 `RunSucceeded` 不对 `OutputT` 泛化。typed output 只通过 `ExecutionEngine[OutputT].run()` 返回；观察事件不得携带实际输出值、`ExecutionResult`、`ExecutionState`、graph node、output engine 或可变 presentation 对象。

目标领域模型为：

```python
@dataclass(frozen=True, slots=True)
class RunCompletionSummary:
    committed: bool
    candidate_id: str | None
    contract_id: OutputContractId | None
    presentation_kind: str | None

@dataclass(frozen=True, slots=True)
class RunSucceeded:
    run_id: str
    summary: RunCompletionSummary
```

具体字段可在 ADR 中因现有领域 ID 类型作等价收窄，但不得重新引入 `OutputT` 或可变对象。摘要是公共观测事实，不承担恢复或结果传输。

### P0：闭合 Agent 依赖与 Toolset 链路

#### R2.1 保留 Toolset 的 AgentDepsT

`AgentDependencies[DepsT, OutputT].toolsets` 不得退化为 `tuple[AnyToolset, ...]`。目标接口必须能静态表达“此 Toolset 可在 `RunContext[DepsT]` 下运行”。

允许 XML/Native 协议在异构容器层形成联合，但联合的每一支必须保留同一个 `DepsT`。概念上应接近：

```text
AgentToolset[DepsT] = XmlToolset[DepsT] | NativeToolset[DepsT]
```

协议兼容性仍需运行时校验，因为 `RoleSchema.command_protocol` 是部署期值，不能完全由静态类型替代。

`AgentDepsT` 采用逆变语义：Toolset 只通过 `RunContext` 消费 Agent dependencies，不生产它。若 `CodingDeps` 是 `CommonDeps` 的子类型，则 `Toolset[CommonDeps]` 可用于 `AgentDependencies[CodingDeps, O]`，反向不成立。

`RunContext[DepsT]` 本身保持不变，因为它公开 `deps: DepsT`，并可通过 `for_tool` 将其作为 projector 输入；不通过改变 `RunContext` 方差来伪造 Toolset 可替换性。Toolset 的生命周期/approval callable 使用逆变的 `AgentDepsT_contra` 表达消费关系。

依赖兼容遵循 Python 静态结构类型规则：依赖建议定义为窄 Protocol；普通类继续遵循名义继承。不得在 Runtime 自建第二套 assignability 算法。动态插件只能做 capability/protocol/schema 的运行时校验，不能完整复现 pyright 的结构子类型判断。

类型契约测试必须包含：

- `Toolset[CommonDeps] -> AgentDependencies[CodingDeps, O]` 通过；
- `Toolset[CodingDeps] -> AgentDependencies[CommonDeps, O]` 失败；
- 两个结构兼容 Protocol 的安全复用通过；
- 缺失所需成员的结构类型失败。

#### R2.2 Toolset 基类的生命周期上下文不得丢型

必须将现有多职责继承层拆为两层：

```text
内部 DefinitionSource[DefinitionT]
公开 XmlToolset[AgentDepsT_contra] / NativeToolset[AgentDepsT_contra]
```

`DefinitionSource` 负责异构 definition 来源、快照与名称唯一性；公开 Toolset 负责协议名义隔离、Agent deps 生命周期和组合代数。不得让一个基类同时承担这四类职责。

具体 capability 类型只在 definition 构造和注册的局部范围保持关联；异构 definition 容器不承诺保留每项 capability 的具体静态类型。公开 Toolset 只有一个领域 TypeVar。

#### R2.3 类型化 approval policy

依赖 Agent context 的 approval policy 不得继续存储在 `ToolDefinition` 上。definition 只描述与 Agent deps 无关的模型及执行定义：schema、capability factory、argument decoder、protocol 和静态 `approval_required`。不得为 definition 增加第二个 `AgentDepsT` 参数。

context-sensitive policy 由 Toolset 持有，并保持逆变：

```python
class ToolsetPolicy(Protocol[AgentDepsT_contra]):
    def requires_approval(
        self,
        context: RunContext[AgentDepsT_contra],
        definition: ErasedToolDefinition,
        arguments: Mapping[str, JsonValue],
    ) -> bool: ...

@dataclass(frozen=True, slots=True)
class BoundApprovalPolicy:
    evaluate: Callable[
        [ErasedToolDefinition, Mapping[str, JsonValue]],
        bool,
    ]
```

在 `for_run(RunContext[DepsT])` 或等价 run binding 阶段，将 `ToolsetPolicy[DepsT]` 与 context 类型安全绑定成不再暴露 `DepsT` 的 `BoundApprovalPolicy`。只有 bound policy 可进入异构 Runtime registry：

```text
ToolsetPolicy[DepsT_contra] + RunContext[DepsT]
  -> type-safe run binding
  -> BoundApprovalPolicy
  -> BoundTool / erased runtime registry
```

filter、prefix、rename、combine、dynamic run view 和 step view 必须保留同一 Toolset policy 语义，不能绕过或重复绑定。

ambient contextvar 可以继续作为运行时 context 传输机制，但 typed approval 必须在进入擦除边界前绑定。`BoundTool.check_permissions()` 不得把 `current_run_context() -> RunContext[Any]` 传给 typed policy，也不得通过 `cast(RunContext[DepsT], ...)` 恢复已擦除类型。context 缺失或绑定失效必须 fail-closed：请求 approval 或拒绝，绝不能放行。

#### R2.4 保持最小权限投影

函数工具必须继续使用显式 projector：

```text
Callable[[AgentDepsT], ToolDepsT]
tool(ToolContext[ToolDepsT], ...)
```

不得为了简化泛型而把完整 `Role`、`RoleState`、环境或 capability map 注入工具。类型改造不能削弱现有最小权限边界。

#### R2.5 动态工具在注册边界校验

MCP、运行时发现工具及 registry 中的异构 capability 可使用擦除后的存储类型，但在注册时必须校验 definition、protocol、capability type 和 schema renderer 的一致性。擦除之后不得再假装恢复某个具体 `CapabilityT`。

注册期 fail-fast 允许改变非法配置的失败时点，但必须由 Toolset ADR 定义稳定异常类型、错误字段、启动语义和迁移测试；不得把原先可成功运行的合法配置变为失败。

### P1：修正 Agent 构造与 catalog 类型

#### R3.1 拆分通用工厂能力与具体构造参数

当前 `AgentFactory.build(type[AgentT], **kwargs: Any) -> AgentT` 必须由两层构造边界取代：

```text
Runtime:
  AgentConstructionRequest
  由 AgentControl 在 spawn transaction 内根据最终 reservation 构造
  仅包含稳定值、领域 ID 与构造策略值

Product:
  AgentBuilder[RequestT_contra, OutputT]
  在组合根提前绑定 Agent 类与 Product 扩展配置
```

目标 builder 形态为：

```python
class AgentBuilder(Protocol[RequestT_contra, OutputT]):
    def build(
        self, request: RequestT_contra
    ) -> RunnableAgent[OutputT]: ...
```

Runtime 不再接收 `type[AgentT] + kwargs` 并通用构造未知 Agent。稳定 request 中禁止 `extras: dict[str, Any]`、任意 kwargs 或动态字段袋；Product 私有配置由已绑定 builder 自身持有。

`Engine` 不以 `ParamSpec` 保留开放式 `Engine.agent(**kwargs)`。若 `Engine` 服务 application/root construction，则使用 Product 定义的显式 root request 与对应单 request builder；它与 child `AgentConstructionRequest` 只是共享 builder 形态，不共享 DTO。若未来确有需要保留多个公开构造签名，必须另立 ADR 证明 ParamSpec 的签名保真价值。

#### R3.2 Catalog 生产预绑定 Agent definition

`SpawnableAgentCatalog` 不再返回裸 Agent class，而是只生产已绑定 builder 的不可变 definition。spawn 链的公开泛型主轴是输出类型 `OutputT`，不是 concrete Agent class；Orchestration 不模拟 Python 不具备的关联类型：

```python
RequestT_contra = TypeVar("RequestT_contra", contravariant=True)
OutputT = TypeVar("OutputT")

class RunnableAgent(Protocol[OutputT]):
    @property
    def session_id(self) -> str: ...

    async def run(
        self, message: Message | None = None
    ) -> RunOutcome[OutputT] | None: ...

    async def cleanup(self) -> None: ...

class AgentBuilder(Protocol[RequestT_contra, OutputT]):
    def build(
        self, request: RequestT_contra
    ) -> RunnableAgent[OutputT]: ...

@dataclass(frozen=True, slots=True)
class SpawnableAgentDefinition(Generic[OutputT]):
    name: str
    aliases: tuple[str, ...]
    description: str
    version: str
    builder: AgentBuilder[AgentConstructionRequest, OutputT]

class SpawnableAgentCatalog(Protocol[OutputT]):
    @property
    def version(self) -> str: ...

    def get(
        self, name: str
    ) -> SpawnableAgentDefinition[OutputT] | None: ...

    def all_agents(
        self,
    ) -> Mapping[str, SpawnableAgentDefinition[OutputT]]: ...
```

`OutputT` 在整条 spawn 链保持不变。当前 `RunOutcome[OutputT]`、`RunResult[OutputT]` 和 `CommittedOutput[OutputT]` 不具备协变保证，因此不得宣称 `SpawnableAgentCatalog[ReviewReport]` 可替换 `SpawnableAgentCatalog[object]`。request 参数 `RequestT_contra` 继续逆变；具体 Agent class 留在 Product builder 内部。construction ADR 必须以 pyright 正反用例验证不变性。

工具只选择 definition 并提交类型化 spawn 计划，不得直接调用 child builder。完整链路冻结为：

```text
Product tool
  agent type name
    -> catalog.get(name)
    -> SpawnableAgentDefinition[OutputT]
    -> SpawnPlan[OutputT]
    -> AgentControl.spawn_agent(plan)

AgentControl（spawn transaction 内）
  admission
    -> reserve residency
    -> reserve identity / nickname / path
    -> construct AgentConstructionRequest from final reservations
    -> definition.builder.build(request)
    -> register cleanup rollback
    -> provision Runtime services
    -> register runtime / supervision while still externally invisible
    -> commit
    -> ChildAgentHandle[OutputT]
```

职责边界固定为：Product 选择“构造哪个 Agent”；Orchestration 决定“何时允许构造”并分配最终身份；Product builder 执行“如何构造”；Runtime/Orchestration 在事务内完成 provisioning、supervision 和 rollback ownership。工具不得提前构造 child，也不得绕过 admission authority。

Runtime、工具和 catalog port 不得接触具体 Agent class、任意 kwargs、Product 私有配置或具体 Agent 构造函数签名。Agent class 可继续用于 Product 内部 discovery、装饰器注册或 source fingerprint，但只能作为 Product builder 的私有实现细节。

`version` 必须是只读 property；definition 的 `name/aliases/description/version` 是稳定 catalog 元数据，不是构造参数袋。

#### R3.3 区分 root construction 与 child construction

两类构造场景不得为了代码复用合并成一个全局 request：

```text
Application/root construction
  Product 组合根直接构造主 Agent
  可消费显式 root request 中的 CLI、应用配置与启动期资源

Child/spawn construction
  Catalog 中的预绑定 SpawnableAgentDefinition
  只消费稳定 AgentConstructionRequest
```

`AgentConstructionRequest` 由 `AgentControl` 在 admission 与 identity/path/residency reservation 完成之后、调用 builder 之前构造。builder 不得接受由工具预制的 construction request，因为工具尚不知道最终 child identity/path，且构造失败必须被同一个 `SpawnTransaction` 接管。

request 只包含稳定值和领域 ID。示意字段为：

```python
@dataclass(frozen=True, slots=True)
class AgentConstructionRequest:
    parent_session_id: str | None
    child_identity: AgentIdentity
    child_path: AgentPathValue
    cwd: str | None
    context_policy: ContextPolicy
```

具体字段由 construction ADR 对照现有领域类型冻结，但禁止包含 `AgentWiring`、`EngineServices`、Runtime Context、CostTracker、Product config、mutable config、具体 Orchestration `AgentPath` 实现、资源所有权对象、`extras` 或开放字段。若构造确需策略，只能传稳定枚举/值；服务所有权与 provisioning 优先由 `ContextPolicy` 和 spawn authority 单一决定，不在 request 中重复建模。

request 不负责 provisioning。root construction 使用独立、显式的 Product request，不要求经过 Runtime catalog port，也不得复用或继承 child request 形成全局构造 DTO。

#### R3.4 闭合 spawn transaction 后的类型链

definition/builder 获得的 `OutputT` 必须贯穿 spawn authority，不能在现有 `SpawnSpec.role_factory: Callable[..., Any]` 处立即丢失：

```text
SpawnableAgentDefinition[OutputT]
  -> SpawnPlan[OutputT]
  -> AgentControl.spawn_agent(...)
  -> RunnableAgent[OutputT]
  -> AgentRuntime[OutputT]
  -> ChildAgentHandle[OutputT]
```

`SpawnPlan[OutputT]` 携带预绑定 definition 或 builder 以及 admission 所需的不可变意图字段，但不携带已构造 Agent、construction request、Runtime service 或任意 callable kwargs。`AgentControl.spawn_agent` 是唯一调用 child builder 的位置。

异构 resident registry 可在明确的注册边界擦除具体 `OutputT`，但只能擦除为具名、稳定的 `SpawnedAgent` Protocol，不能擦除为 `Any`：

```python
class SpawnedAgent(RunnableAgent[OutputT], Protocol[OutputT]):
    @property
    def session_id(self) -> str: ...

    async def cleanup(self) -> None: ...

    # ADR 只补充 AgentControl/AgentRuntime 真正依赖的稳定能力。
```

静态 definition 路径必须保持具体 `OutputT`：`SpawnableAgentDefinition[ReviewReport] -> SpawnPlan[ReviewReport] -> ChildAgentHandle[ReviewReport]`。

动态字符串 catalog lookup 无法静态恢复异构 Agent 的不同输出类型，必须在 lookup 处通过真实 erased adapter 包装为 `SpawnableAgentDefinition[object]`、`ChildAgentHandle[object]`，或明确的封闭输出联合。由于 `OutputT` 不变，该 adapter 不能是赋值或 cast；它必须包装 builder/runnable/handle，并只向外暴露 `RunOutcome[object]`。该擦除点必须登记到 `dynamic-boundaries.json`；不得根据 output contract ID、runtime class 或 cast 恢复具体 `OutputT`。run-agent 工具可在此边界后按 output contract/领域 encoder 运行时序列化，但这不等于静态类型恢复。

异构 registry 若需擦除，也必须明确擦除为 `SpawnedAgent[object]` 或稳定非泛型监督 Protocol，并记录擦除函数与位置。擦除后不得通过 `getattr` 猜测 cleanup、spawn snapshot、provisioning 或 run 能力。

构造出的 Agent 必须立即把 cleanup 注册给 `SpawnTransaction`；provisioning 完成前不得进入外部可见 registry。构造、provisioning、runtime 注册或 supervision 任一步失败，都必须由同一 transaction 逆序回滚已获得资源。

“spawned”状态只能由 `AgentControl` 在 transaction commit 后发布。builder、工具、definition、runtime wrapper 或 registry adapter 均无权提前标记 spawned，且不得暴露尚未完成 provisioning 的 child。

#### R3.5 Engine 生命周期协议化

`Engine[AgentT]` 不应通过 `getattr(agent, "cleanup")` 隐式假设生命周期。应定义最小的可关闭 Agent Protocol，或向 Engine 注入 disposer。若允许不带 cleanup 的 Agent，则应把这种选择体现在工厂/策略类型中，而非运行时猜测。

### P1：类型化内部事件与展示管线

#### R4.1 Telemetry 核心保持诚实擦除，只类型化边缘

通用 `TelemetryRuntime` 是跨层异构传输核心，内部合法并明确使用 `object`，只负责容量、背压、投递、隔离和生命周期。不得将其泛化为全局封闭事件联合，也不得让 Contracts/Runtime 为枚举 Product 事件反向 import 高层。

必须类型化的是相邻边缘，并把事件 narrower 作为 binding 的一等契约：

```text
TypedTelemetryPublisher[EventT]
  某一层可发布事件的类型化入口

TelemetryHandler[EventT_contra] / SyncTelemetryHandler[EventT_contra]
  类型化 async/sync 消费入口

EventNarrower[EventT_co]
  object -> TypeGuard[EventT_co]

TypedTelemetryBinding[EventT]
  spec + accepts + async handler + optional sync handler

TelemetryRuntime
  object 擦除的异构传输核心
```

目标模型为：

```python
class TelemetryHandler(Protocol[EventT_contra]):
    async def handle(self, event: EventT_contra) -> None: ...

class SyncTelemetryHandler(Protocol[EventT_contra]):
    def handle_sync(self, event: EventT_contra) -> None: ...

EventT_co = TypeVar("EventT_co", covariant=True)
EventT = TypeVar("EventT")

class EventNarrower(Protocol[EventT_co]):
    def __call__(self, event: object) -> TypeGuard[EventT_co]: ...

@dataclass(frozen=True, slots=True)
class TypedTelemetryBinding(Generic[EventT]):
    spec: TelemetrySubscriptionSpec
    accepts: EventNarrower[EventT]
    handler: TelemetryHandler[EventT]
    sync_handler: SyncTelemetryHandler[EventT] | None = None
```

`EventNarrower` 只通过 `TypeGuard` 生产事件类型，因此其 `EventT_co` 显式协变。`TypedTelemetryBinding[EventT]` 同时通过 narrower 生产并通过 handler 消费事件，必须保持不变；不得将 binding 整体声明为协变或逆变。

注册时由 binding 创建唯一可进入 `TelemetryRuntime` 的 erased adapter。adapter 先执行同一个 `accepts` narrower，成功后才分别调用 async/sync typed handler。具体 handler 永远不能收到未通过 `TypeGuard` 的事件，runtime-checkable generic Protocol 不能替代该过滤。

同步语义冻结为：绑定未提供 `sync_handler` 时，该绑定跳过同步投递；不得通过 `getattr` 猜测、隐式转入 async mailbox 或阻塞等待 async handler。异步投递只调用 async handler。两条路径必须使用同一个 narrower，并分别有测试。

类型擦除只发生在 typed binding 生成 erased adapter 的位置；总线容器自身不承诺跨层封闭性。

#### R4.2 Projector 泛化输入与输出

通用 projector 应表达 `Projector[InputEventT, ViewEventT]`，返回只读 `Sequence[ViewEventT]` 或具体不可变集合。对输入只消费、对输出只生产时应使用正确方差。

Product presentation 必须定义其真正支持的 `PresentationInputEvent` 联合，只列出 presentation 消费的 Contracts 事件。`ViewProjector` 固定为 `Projector[PresentationInputEvent, ViewEvent]`；未知 telemetry event 在进入 projector 前由 binding narrower 过滤，不再由 projector 通过 `Any + getattr` 猜测字段。

该联合属于 Product presentation，不是 Contracts 全局事件联合；新增与展示无关的 telemetry event 不要求修改它。事件 payload 内部仍合理存在的动态字段留给 R5 治理，不要求阶段 D 一次性泛化全部 domain events。`AgentEvent = Any` 不得继续作为 presentation 主链便利类型。

#### R4.3 Consumer 泛化消费事件

`Consumer[EventT_contra]` 应只消费其声明的事件类型。registry/fan-out 可保存擦除后的 consumer，但注册边界必须校验或由类型化 API 构造，不能让 `Any` 从 registry 反向污染具体 consumer。
ACP、AG-UI 等 wire adapter 只能接收其声明的 `ViewEvent` 或机器 view 联合，不得直接消费原始 telemetry `object`。

#### R4.4 不强行统一 domain event、view event 与 wire payload

三类事件必须保持分层：

- domain/telemetry event：内部事实；
- view event：面向交互展示的投影；
- ACP/JSON/gRPC payload：外部 wire 表示。

泛型用于连接相邻变换，不建立一个覆盖三层的万能 `Event[T]`。

### P1：修正已有泛型与 Protocol 缺陷

至少处理静态检查已暴露的以下问题类别：

- Protocol 方差声明不正确；
- Protocol 的只读/可写属性与实现不一致；
- 回调声明为 `Awaitable[T]`，调用方却要求 `Coroutine[Any, Any, T]`；
- `Callable[..., object]` 掩盖同步/异步返回约束；
- `dict`、`list`、`tuple` 缺少参数；
- `ContextVar`、token、task、queue 等状态字段由初值错误推断成过窄类型；
- 能力 map 的 NewType/领域 ID 与裸 `str` 漂移；
- Protocol 方法参数名不一致导致结构化不兼容。

这些问题不一定都需要新增泛型；应选择最小且语义正确的修复。

### P2：治理持久化、外部协议与动态配置边界

#### R5.1 使用 JsonValue 替代任意 JSON

已存在 `JsonValue` 的领域应优先使用它，不应继续新增 `dict[str, Any]`。可变输入可在 codec 边界接收普通 JSON 容器，领域内转换为冻结值。

#### R5.2 为外部 SDK 建立 adapter 类型

Playwright、Textual、OpenAI/Anthropic、gRPC 生成代码等第三方对象允许保留局部 `Any`，但不得穿透 adapter 进入 Kernel 或 Contracts。

#### R5.3 配置扩展点使用窄 Protocol

`hook_config`、MCP server config、routing builder 等若已有稳定语义，应迁移为 Contracts 配置模型或窄 Protocol。尚未稳定的插件载荷可以保留 `object`，由具体 Product adapter 解析。

## 6. 建议目标类型关系

以下关系用于评审方向，不强制逐字采用命名：

```text
AgentDependencies[DepsT, OutputT]
  ├── deps: DepsT
  ├── output_contract: OutputContract[OutputT]
  └── toolsets: tuple[AgentToolset[DepsT], ...]

Role[DepsT, OutputT]
  ├── wiring: AgentWiring[DepsT, OutputT]
  ├── run context: RunContext[DepsT]
  └── run(...) -> RunOutcome[OutputT] | None

ExecutionEngine[OutputT]
  ├── output operation: OutputOperation[OutputT]
  ├── graph: AgentGraph[ExecutionState[OutputT], ExecutionResult[OutputT] | None]
  └── run() -> ExecutionResult[OutputT] | None

DefinitionSource[DefinitionT]
  └── 内部 definition 存储、快照与名称唯一性

Toolset[AgentDepsT_contra]
  ├── for_run(RunContext[AgentDepsT_contra])
  ├── for_run_step(RunContext[AgentDepsT_contra])
  └── policy: ToolsetPolicy[AgentDepsT_contra]

ToolsetPolicy[AgentDepsT_contra] + RunContext[AgentDepsT]
  -> BoundApprovalPolicy
  -> erased BoundTool registry

Function tool:
  AgentDepsT -> projector -> ToolDepsT -> ToolContext[ToolDepsT]

Projector[InEventT_contra, OutEventT_co]
  project(InEventT_contra) -> Sequence[OutEventT_co]

Consumer[EventT_contra]
  handle(EventT_contra) -> None

SpawnableAgentCatalog[OutputT]
  -> SpawnableAgentDefinition[OutputT]
  -> SpawnPlan[OutputT]
  -> AgentControl transaction
  -> AgentBuilder[AgentConstructionRequest, OutputT]
  -> RunnableAgent[OutputT]
  -> AgentRuntime[OutputT]
  -> ChildAgentHandle[OutputT]
```

## 7. 明确禁止的设计

评审与实现阶段禁止采用：

1. 一个覆盖 Agent、工具、事件、输出的全局 `T`；
2. `Role[DepsT, OutputT, ToolT, EventT, ConfigT, ...]` 式参数爆炸；
3. 仅为消除 `Any` 而把它替换成无约束 `TypeVar`；
4. 通过 `cast` 把擦除后的异构 registry 伪装成同构集合；
5. 通过局部 import 规避泛型改造暴露出的循环依赖；
6. 在 Contracts 引入 Runtime/Product 类型；
7. 把 runtime-checkable Protocol 当成完整的运行时泛型校验；
8. 让公开 API 暴露内部 graph node、具体 executor 或第三方 SDK 类型；
9. 以兼容名、re-export、旧新双接口长期保留迁移残渣；
10. 在未建立稳定关系前引入 `ParamSpec`、`TypeVarTuple` 等复杂类型工具。只有真实保留 callable 签名时才允许使用 `ParamSpec`。

## 8. 实施阶段

### Gate 0：语言与检查环境契约

- 将最低 Python 版本统一为 3.11 并修正 metadata/classifiers/CI；
- 精确锁定 pyright；
- 建立各岛独立配置、权威 CI 命令和正负类型用例；
- 建立逐条诊断例外清单和动态边界登记。

### Gate 1：类型 ADR

- 批准 typed output/run event ADR；
- 批准 Toolset 方差/擦除/注册 ADR；
- 批准 Agent construction/builder ADR；
- 批准 Telemetry core/typed edge ADR。

### 阶段 A：typed output 主链闭合

- 泛化 `ExecutionEngine`、`OutputOperation`、graph builder 和节点；
- 替换 `ExecutionState.turn: Any`；
- 类型化 `GraphAssemblyInputs`；
- 拆分并泛化 `OutputTransactionPort[OutputT]`，保持非输出 transaction port 非泛型；
- 补齐 Kernel 所需的窄 output evaluation/state/restore ports；
- 确保 `Role[D, O].run()` 的 `O` 来自同一个 output contract；
- 完成 flow、roles、output 相关测试。

### 阶段 B：Agent deps 与 Toolset 闭合

- 重塑 Toolset 泛型，使 `AgentDepsT` 贯穿生命周期和 approval；
- 修正 `AgentDependencies.toolsets`；
- 保持 XML/Native 名义隔离和动态协议校验；
- 为异构 capability registry 明确擦除边界；
- 将 context-sensitive approval 从 definition 移到 `ToolsetPolicy[AgentDepsT_contra]`；
- 在 run binding 阶段生成 `BoundApprovalPolicy`，禁止 ambient `RunContext[Any]` 进入 typed policy；
- 完成 executor、toolset、dynamic/function toolset 测试。

### 阶段 C：工厂与多 Agent 边界

- 分离 application/root construction 与 child/spawn construction；
- 将 catalog 从裸 Agent class 迁移到预绑定 `SpawnableAgentDefinition`；
- 将工具改为提交 `SpawnPlan[OutputT]`，禁止直接调用 builder；
- 由 `AgentControl` 在 reservation 后、transaction commit 前构造 `AgentConstructionRequest` 并调用 builder；
- 移除 Runtime/tool-facing 的 `type[AgentT] + **kwargs` 和 `Callable[..., Any]` 构造链；
- 以 `OutputT` 泛化 `SpawnPlan`、`RunnableAgent`、`AgentRuntime`、`ChildAgentHandle`；动态 name lookup 只允许显式擦除为 `object` 或封闭联合；
- 用具名生命周期/构造 Protocol 消除 cleanup、spawn snapshot 和 provisioning 的反射猜测；
- 修正 builder、catalog、Engine 生命周期接口；
- 确保静态 definition 的输出类型贯穿 spawn、runtime、handle 和 run outcome；
- 完成 orchestration、runtime lifecycle、product factory 测试。

### 阶段 D：事件与 presentation typed edge

- 保持 Telemetry core 的 `object` 擦除，类型化 publisher、handler、projector、consumer；
- 引入携带 `TypeGuard` narrower 的 `TypedTelemetryBinding`，仅 erased adapter 进入广播核心；
- 显式建模 async/sync handler，并采用“缺少 sync handler 时跳过该绑定的同步投递”语义；
- 定义 Product-owned `PresentationInputEvent` 联合，移除 presentation 主链的 `AgentEvent = Any`；
- 将 domain/view/wire 三层转换边界显式化；
- 对 registry 保留有限、受控的类型擦除；
- 完成 events、presentation、CLI 接口测试。

### 阶段 E：扩大严格检查覆盖

- 将已完成区域切换到 strict 或等价规则集；
- 逐步纳入其直接依赖方；
- 清理无依据 ignore/cast；
- 文档化剩余动态边界及其理由。
- 已治理类型岛必须删除临时诊断例外，不永久维护历史 baseline。

## 9. 验收标准

### 9.1 功能与架构

- 正常成功路径、wire schema、持久化语义、权限结果和协议结果保持不变；边界校验允许经对应 ADR 批准后变为更早、确定性的失败，但必须定义异常契约、启动语义和迁移测试；
- 不新增反向依赖或局部 import；
- `contracts <- kernel <- runtime <- orchestration <- product` 架构测试通过；
- 工具仍只能获得显式依赖投影和 capability allowlist。

### 9.2 静态类型

- 自定义结构化输出可从 `Role[D, O]` 静态推断为 `RunOutcome[O] | None`；
- 错误的 output engine/contract/graph 组合在类型检查时失败；
- `OutputTransactionPort[A]` 不能注入 `OutputOperation[B]`；
- terminal commit 无需 cast 即推断为 `CommittedOutput[O]`，restore 推断为 `ExecutionResult[O] | None`；
- typed output 主链不存在 `AcceptedOutput[Any]` 或 `CommittedOutput[Any]`；
- `Toolset[ADeps]` 不能被配置给不兼容的 `AgentDependencies[BDeps, O]`；
- function tool 的 projector 返回错误依赖类型时检查失败；
- XML 与 Native Toolset 交叉组合继续静态或构造期失败；
- ToolDefinition 不持有接收 `RunContext[D]` 的 callback；
- `ToolsetPolicy[CommonDeps]` 可服务 `CodingDeps`，反向组合失败；
- bind 后的 `BoundApprovalPolicy` 不暴露 `DepsT`，ambient `RunContext[Any]` 不进入 typed approval；
- catalog 只能返回 `SpawnableAgentDefinition[OutputT]`，不能返回裸 Agent class；
- child builder 只能接受 `AgentConstructionRequest`，传入 root/CLI 配置或开放 kwargs 时检查失败；
- Product 工具不能直接调用 child builder，只能向 `AgentControl` 提交类型化 spawn plan；
- 未经 `AgentControl` transaction commit，任何调用方都不能把 child 标记为 spawned 或使其进入可发现 registry；
- `SpawnPlan[OutputA]` 不能装入生产 `OutputB` 的 builder；
- 静态 definition 路径的输出类型不能在 `SpawnPlan`、`AgentRuntime` 或 `ChildAgentHandle` 处退化为 `Any`；
- 动态 name lookup 必须显式得到 `object` 或封闭联合，不得从 contract ID/runtime class cast 回具体输出类型；
- `SpawnableAgentCatalog[ReviewReport]` 不能赋给 `SpawnableAgentCatalog[object]`；动态擦除必须经过真实 wrapper/adapter；
- `AgentConstructionRequest` 不能包含 `EngineServices`、`AgentWiring`、CostTracker、mutable/Product config、具体 runtime object 或开放参数袋；
- projector 输入/输出接错、consumer 消费错误 view event 时检查失败；
- `TelemetryHandler[A]` 不能绑定 `EventNarrower[B]`；只有 erased adapter 可注册到 Telemetry core；
- `EventNarrower` 的事件参数协变，而 `TypedTelemetryBinding` 保持不变；错误 narrower/handler 组合必须静态失败；
- `ViewProjector.project()` 只接受 `PresentationInputEvent`，wire adapter 只接受声明的 view union；
- 已治理目录不存在未参数化泛型、public unknown、无 rule 的 ignore 或未登记的跨边界 `Any/object` 擦除；
- 核心链路不再使用 `ExecutionState[Any]`、`ExecutionResult[Any]`、`RunContext[Any]` 作为便利性默认值。

### 9.3 测试

- 每个阶段至少运行对应子系统及直接依赖方的 ztest；
- 增加静态正例与负例测试。负例应验证类型检查必然失败，而不是只写注释；
- typed output、toolset composition、dynamic tool lifecycle、Agent spawn、event projection 均有运行时回归测试；
- Runtime transaction 同时满足非泛型 execution port 与对应的泛型 output port；
- filter/prefix/rename/combine 和 dynamic run/step view 均保留 Toolset approval policy；
- approval context 缺失时 fail-closed，绝不放行；
- child 构造失败后 residency、identity、nickname 和 path reservation 全部释放；
- provisioning 失败时已构造 Agent 必须 cleanup，且不得进入外部可见 registry；
- runtime 注册或 supervision 失败时已注册的 inert runtime、cost node、communication graph 和 incarnation 必须逆序撤销；
- registry 对外可见与 spawned 事件只能发生在 provisioning 和 supervision 成功之后；
- cleanup、spawn snapshot 与 provisioning 不再通过未声明的 `getattr` 猜测；
- 广播错误事件类型时 typed handler 不被调用；async/sync 使用相同 narrower，且缺少 sync handler 的跳过行为有测试；
- 未知 telemetry event 在 projector 前被过滤，projector 不再以 `Any + getattr` 解释已知事件；
- pyright 检查结果可在本地和 CI 重复，缺失依赖不污染产品错误统计。

### 9.4 可维护性

- 核心公开类原则上不超过两个领域 TypeVar；超过时必须在 ADR 中说明不可拆分原因；
- 新增 Protocol 必须是窄接口，并至少满足一项：跨层端口、两个独立生产实现、已确认的替换策略，或隔离外部系统的稳定测试 seam；测试替身本身不能证明抽象必要；
- 每个保留的核心 `Any` 均能指出动态来源、校验位置和不能进一步收窄的原因；
- 不以显著增加调用方注解负担为代价。常用文本 Agent 应继续可通过默认构造获得 `Role[None, str]` 或等价推断。

## 10. 已冻结的架构裁决

本需求采纳并冻结以下结论，实施者不得自行改选：

1. **执行事件**：不泛化；`RunSucceeded` 只携带不可变稳定摘要和领域 ID。
2. **Toolset**：公开 `XmlToolset[AgentDepsT_contra]` / `NativeToolset[AgentDepsT_contra]`；内部 `DefinitionSource[DefinitionT]` 单独泛化；context-sensitive approval 属于 `ToolsetPolicy`，在类型擦除前与 `RunContext` 绑定为不暴露 deps 的 `BoundApprovalPolicy`。
3. **Agent construction**：root construction 留在 Product 组合根；spawn 链以不变 `OutputT` 为泛型主轴。Product 工具只选择预绑定 `SpawnableAgentDefinition[OutputT]` 并提交 `SpawnPlan[OutputT]`；`AgentControl` 在 reservation 后、同一 spawn transaction 内构造纯值 `AgentConstructionRequest`、调用 builder、接管 cleanup/provisioning/supervision 并提交；动态 name lookup 必须用真实 wrapper/adapter 擦除为 `object` 或封闭联合，禁止协变赋值、cast 或从 contract ID/class 恢复类型。
4. **ExecutionState**：`NoModelTurn | ModelTurn | CandidateSelection`，不使用 `None` 或 tuple 承载状态语义。
5. **类型检查器**：pyright 是唯一权威检查器并精确锁版，不同时维护 mypy。
6. **Python 版本**：最低版本立即统一到 3.11，并采用滚动支持和独立 ADR 政策。
7. **Telemetry**：通用传输核心使用 `object` 诚实擦除；`EventNarrower[EventT_co]` 协变，`TypedTelemetryBinding[EventT]` 不变；binding 必须携带 `TypeGuard` narrower 与显式 async/optional sync handler，只有 erased adapter 可进入 core；缺少 sync handler 时该绑定跳过同步投递。
8. **诊断债务**：永久负例契约使用 `case-expectations.json`；临时产品债务使用带 owner/expiry 的 `migration-diagnostics.json`，类型岛治理完成后清零。
9. **Output transaction**：非输出 execution transaction 保持非泛型；accepted/staged/terminal output 通过独立 `OutputTransactionPort[OutputT]` 闭合。

## 11. 风险与控制

### 风险 1：泛型参数扩散导致 API 难用

控制：仅在关联关系上引入 TypeVar；通过类型别名、默认 builder 和局部 Protocol 隐藏内部复杂度；限制公开类 TypeVar 数量。

### 风险 2：静态设计与动态插件系统冲突

控制：允许 registry 内部类型擦除，但将校验前移到注册边界；不声称静态检查可以验证运行时加载的未知插件。

### 风险 3：一次性 strict 产生大量无价值修复

控制：以类型岛和债务棘轮推进；优先闭合高价值主链，再扩大覆盖。

### 风险 4：类型改造意外改变持久化或恢复语义

控制：不让 TypeVar 进入 wire schema；对 rollout、checkpoint、event replay 和 output migration 做回归测试。

### 风险 5：用 Protocol 拆分造成接口数量爆炸

控制：只为跨层端口或可复用窄能力建 Protocol；模块内部单实现协作者可直接使用具体类型。

## 12. 完成定义

本需求完成不是指“仓库中没有 `Any`”，而是指：

- Agent deps、typed output、Toolset、执行图、工厂和事件投影的关键类型关系端到端闭合；
- 动态性被限制在明确边界，并有验证与收窄；
- 类型检查成为可重复、可渐进强化、不可倒退的工程门禁；
- 新扩展能够复用这些关系，而不需要复制 `cast`、扩大 capability、绕过分层或引入新的兼容债务。
