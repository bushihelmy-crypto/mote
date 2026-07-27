# Toolset 协议隔离架构

> 状态：核心架构已落地，剩余项见第 13 节。本文既是当前实现说明，也是后续演进约束；实现若与本文冲突，应修改实现，而不是在协议边界增加兼容分支。

## 1. 目标

Mote 的 Toolset 是面向模型的能力集合，不是 `BaseTool` 类的分类列表。它必须同时满足：

- XML 与 provider-native tool use 具有独立、可静态检查的类型边界；
- 相同执行能力可以复用，但暴露给不同协议必须显式注册；
- 过滤、前缀、审批、动态准备和组合不会绕过 Runtime 的权限与副作用控制平面；
- Agent 在装配期确定唯一命令协议，错误协议立即失败；
- MCP、provider-native tools、server-side tool search 等 Native 语义不会泄漏到 XML；
- 新协议可以增加自己的 definition/toolset，而不修改已有协议的参数模型。

这不是给现有双投影接口增加 `protocol=` 参数。目标是删除“双投影”这一错误抽象。

## 2. 核心判断

XML command 与 Native tool call 不是同一种工具定义的两种序列化格式。

| 维度 | XML | Native |
| --- | --- | --- |
| 调用载体 | assistant 文本中的 XML command block | provider tool-call envelope |
| 参数语义 | XML parser 产出的字符串标量 | JSON Schema 约束的结构化 JSON |
| 工具描述 | per-turn prompt catalog | API `tools=` |
| deferred | client-side withhold/reveal | stable spec、split description 或 server-side defer |
| 动态工具 | 必须有显式 XML adapter | MCP/provider-native lifecycle |
| final output tool | 不存在；用 XML end/candidate | 可使用 native semantic final tool |
| provider 扩展 | 不存在 | tool choice、references、tool search、built-in tools |

如果让一个对象同时提供 `tool_schema()` 与 `native_schema()`，类型系统无法表达它究竟属于哪个协议，组合器也无法阻止混装。此后任何新 Native 特性都会继续污染 XML 抽象。

## 3. 分层模型

```text
contracts
  CommandProtocol
  XmlToolSchema / NativeToolSchema        # 纯数据契约

kernel
  XmlToolDefinition[CapabilityT]
  NativeToolDefinition[CapabilityT]
  XmlToolset[CapabilityT]
  NativeToolset[CapabilityT]
  同协议 composition algebra

runtime
  ToolCapability                          # 执行、权限元数据、资源清理
  BoundTool                               # definition + capability instance
  XmlToolCatalog / NativeToolCatalog      # 协议专属模型视图
  ToolExecutor                            # 共享安全执行控制平面
  Shared MCP connection owner             # 协议无关连接/发现/调用
  XmlMcpToolset / NativeMcpToolset        # 显式 definition 投影

product
  XML_BUILTIN_TOOLSETS
  NATIVE_BUILTIN_TOOLSETS
  显式的双协议注册与产品组合
```

依赖仍严格遵循：

```text
contracts <- kernel <- runtime <- orchestration <- product
```

Kernel definition 对 Runtime capability 使用泛型句柄，不 import `BaseTool`。Runtime 在绑定阶段把句柄解释为 capability factory。Product 决定一个具体 capability 暴露在哪些协议。

## 4. 类型模型

### 4.1 执行能力不拥有 wire schema

执行能力负责：

- `call()`；
- 权限目标与自检；
- effect、filesystem mutation、reconstructable 等执行元数据；
- session bind/cleanup；
- Runtime capability 最小权限注入。

执行能力不再负责：

- XML schema；
- Native JSON Schema；
- provider envelope；
- deferred wire 标记；
- model-facing rename/prefix。

因此目标状态中 `BaseTool.tool_schema()`、`BaseTool.native_schema()`、`BaseTool.get_native_schema()` 全部删除。`BaseTool` 最终改名为更准确的 `ToolCapability` 可单独进行；本次协议迁移不应靠保留双 schema 方法维持兼容。

### 4.2 Definition 是名义类型

概念形态如下：

```python
@dataclass(frozen=True, slots=True)
class XmlToolDefinition(Generic[CapabilityT]):
    name: str
    capability: CapabilityFactory[CapabilityT]
    description: str
    parameters: tuple[XmlParameter, ...]


@dataclass(frozen=True, slots=True)
class NativeToolDefinition(Generic[CapabilityT]):
    name: str
    capability: CapabilityFactory[CapabilityT]
    description: str
    input_schema: JsonSchema
    provider_options: NativeToolOptions
```

两者不能继承一个含 `schema()` 的公共 definition 基类。可以共享仅含执行身份的内部 Protocol，但 wire 字段必须保持分离。

XML 参数 definition 只允许 parser 能忠实表达的类型。第一阶段为字符串标量及可选性；禁止把 `list`、`dict`、Pydantic model 的 JSON Schema 假装成 XML 参数契约。Native definition 保留完整 JSON Schema。

### 4.3 Toolset 是协议专属代数

公开类型：

```python
XmlToolset[CapabilityT]
NativeToolset[CapabilityT]
```

共同拥有概念上相似但类型签名不同的操作：

```python
xml.filter(...).prefix(...).with_approval(...).combine(other_xml)
native.filter(...).prefix(...).with_approval(...).combine(other_native)
```

静态检查器必须拒绝：

```python
xml.combine(native)
native.combine(xml)
```

Runtime 也必须验证协议 tag，防止未运行静态检查的 Python 调用绕过边界。不能静默过滤错误成员。

`Toolset` 可以作为 facade 中的只读联合概念，但不能作为 `combine()` 的宽泛参数类型，也不能再表示“可投影成任意协议的集合”。

## 5. Composition algebra

所有变换均为不可变视图，不复制 capability，也不改变执行流水线。

### `filter(policy)`

按 definition/capability metadata 过滤。policy 不接触已绑定 Runtime 对象，不产生隐藏副作用。

### `prefix(namespace)` / `rename(mapping)`

只改变该协议的 model-facing name 和 dispatch registration。原始 capability identity 保持不变。XML 与 Native 分别重建自己的 definition，不存在同时重写两个 schema 的 wrapper。

### `with_approval(policy)`

无 policy 时给选中的 definition 增加固定审批要求；传入 policy 时，其类型化签名接收 `RunContext[DepsT]`、当前协议 definition 和本次调用参数，返回本次调用是否需要审批。`mutating_only=True` 可继续作为 definition 级预筛选。

policy 是同步纯判定，不负责 IO，更不能直接调用 `ask_user`；调用参数以只读 mapping 传入，不能借 policy 绕过 hook rewrite/audit 去改写实际 dispatch。最终 ask/deny 仍由中央 Permission gate 执行，Toolset 不能绕过 deny、sandbox、hook 或 effect ledger。没有 active `RunContext` 时 predicate 不会以伪造 deps 执行，而是 fail-closed 为需要审批；多个 approval wrapper 按 OR 合并。async policy 在组合边界拒绝，非 bool 返回在执行边界 fail-closed。`prepared()` 不得修改或移除既有 approval flag/predicate。

### `with_instructions(*blocks)`

给当前协议的 Toolset 增加使用约束，保持原 definition 与 capability identity 不变。指令块按组合顺序稳定去重，并分为两种缓存语义：

- 配置在静态 Toolset 或 wrapper 上的 session-static instructions 进入 system prompt 的 `# Toolset instructions`；
- dynamic Toolset 当前 inner 提供的 per-run/per-step instructions 进入 request-only `<system-reminder>`，不写入历史。

Combined view 分别合并静态与动态指令，不能因为一个 child 是动态的，就把其他静态 child 的指令全部降级到 reminder。MCP hot-load catalog 继续使用既有 SR 通道；本能力不会读取或信任远端 MCP server instructions，也不会改变 XML builtin schema 位于 SP 的规则。

### `combine(other)`

仅同协议可组合。组合在装配期检查最终呈现名称冲突；两个集合即使指向同一个 capability，只要占用相同名称也必须报错，不能依赖顺序覆盖。

### `prepared(callback)` / dynamic

`prepared(callback)` 是不可变 definition 视图：允许过滤 definition 或调整描述/schema metadata，但禁止增加工具、重命名 dispatch name、替换 capability 或改变协议。重命名只能显式使用 `rename({old: new})`。

`XmlDynamicToolset[DepsT]` 与 `NativeDynamicToolset[DepsT]` 的 factory 接收 `RunContext[DepsT]`，可选择 per-run 或 per-step 计算。Runtime 在每次 readiness 物化中只读取一次每个 Toolset，随后使用同一 dispatch snapshot 完成冲突检查与 capability 绑定，避免动态 discovery 被重复触发。

动态 wrapper 传播组合视图的 run/step 语义和 async context lifecycle。per-step 切换先退出旧 inner，再进入新 inner；factory 或 enter 失败不会把 `ToolExecutor` 永久标记为 active。XML 与 Native dynamic Toolset 不共享 definition、catalog 或注册槽。

## 6. Agent 装配

`AgentDependencies` 保存协议化 Toolset，不保存可双投影的 Toolset：

```text
AgentSpec.command_protocol
          +
AgentDependencies.toolsets
          |
          v
validate_toolset_protocols()  -- Agent/Role 构造期执行
          |
          v
protocol-specific catalog + shared executor
```

不变量：

1. 一个 Agent run 只有一个 command protocol；
2. 所有注入 Toolset 必须与该协议一致；
3. Product 默认 Toolsets 由 command protocol 选择，不把两套一起注入；
4. fork、spawn、resume、rehydrate 继承已经验证过的不可变依赖包；
5. protocol 与 toolsets 不匹配时在 Agent/Role 装配期抛出 `ToolsetProtocolError`，不能等第一次生成 prompt 或第一次调用工具。

公开 `Engine.agent(...)` 应显式接受 `command_protocol`，并据此选择 Product Toolsets。默认仍可为 Native，但默认值不能意味着“同一 Toolset 自动兼容 Native”。

### 6.1 Durable identity 与恢复契约

每个 Toolset 拥有纯 contracts DTO `ToolsetIdentity(id, version, protocol)`。`id` 标识逻辑能力集合，`version` 是应用负责维护的行为语义版本，`protocol` 是身份的一部分；即使 XML 与 Native 使用相同 id/version，也不是同一个 durable identity。基础、Function、Dynamic、Registry 与 MCP Toolset 均可显式指定 version；不可变 view 继承源版本，Combined 使用有序 child identity 的规范 JSON 计算固定长度 SHA-256 版本，因此 child 的 id/version/protocol 或顺序变化都会改变组合身份。

`AgentDependencies.toolsets` 的有序 manifest 写入 rollout 第一条 `SessionMetaEvent`，不进入 `RoleState`，也不建立第二套持久化真相源。fork 原样继承该 manifest；普通 resume 与 Residency unload/rehydrate 都经 `BaseRole.validate_resume_identity()` 的同一正式边界比较当前依赖与记录值，任何已知 id、version、protocol 或顺序不匹配均以 `SessionResumeIdentityError` fail-closed。Residency 的 `AgentIncarnationFactory` 另外锁定 snapshot role type、具体类、配置和原子 wiring；它是进程内顺序 replacement blueprint，不序列化依赖对象，也不冒充跨进程恢复格式。旧 rollout 缺少 manifest 时允许单向兼容，但新日志必须明确记录空 manifest `[]`，从而区分“旧格式未知”与“确认没有 Toolset”。

动态 Toolset 的当前 definition catalog 不写入 manifest：per-step capability 变化和 MCP 热加载本来就是运行时状态。manifest 标识的是产生这些 catalog 的稳定 wiring/factory 语义；factory、filter/prepared/approval policy 或远端配置发生不兼容改变时，所有者必须提升基础 Toolset version。这样既不会把热加载冻结成快照，也不会在恢复时悄悄换掉已知执行语义。

## 7. 解析后汇合，而不是定义前混合

```text
XmlToolset -> XML catalog -> XmlCommandChannel -> Xml parser ----+
                                                               |
NativeToolset -> Native specs -> provider -> Native parser -----+-> ToolInvocation
                                                                      |
                                                                      v
Permission -> Effect ledger -> Snapshot -> Execute -> Settlement -> Audit
```

共享对象是协议无关的语义调用：

```python
ToolInvocation(
    name: str,
    arguments: Mapping[str, object],
    call_id: str | None,
)
```

XML parser 在构造 invocation 时完成字符串参数语义；Native parser 保留 JSON 值。`ToolInvocation` 之后的权限、effect ledger、snapshot、recovery、result limiting、settlement 和 audit 必须只有一套。

不得为 XML 和 Native 分裂两个 `ToolExecutor` 实现，也不得在 tool body 内判断 command protocol。

## 8. Function Toolset 与 ToolContext

删除含糊的 `FunctionToolset`，改为显式构造：

```python
xml_tools = XmlFunctionToolset[AppDeps]("legacy")
native_tools = NativeFunctionToolset[AppDeps]("structured")
```

二者可以共享内部 function capability adapter 和 `RunContext.for_tool(projector)`，但 schema builder 完全独立：

- `XmlFunctionToolset` 注册时验证除 `ToolContext` 外的参数都能映射为 XML 标量；
- `NativeFunctionToolset` 使用完整 JSON Schema；
- 同一函数若要暴露给两种协议，调用方必须分别注册两次；
- projector 始终是必填项，工具只能获得 `ToolContext[ToolDepsT]`。

## 9. MCP 与 provider-native tools

MCP 是能力发现与调用协议，不等同于模型的 command protocol。它的一等集成必须同时提供两个显式、隔离的适配器：

- `NativeMcpToolset`：discovery 产出 `NativeToolDefinition`，原样保留 MCP JSON Schema；
- `XmlMcpToolset`：discovery 产出 `XmlToolDefinition`，只接受 XML 可表达的标量参数，并在执行前把 XML 字符串按显式 schema 解码；
- 两种协议的全部 MCP definition catalog 都通过 `<system-reminder>` 增量发布，以承载热加载；XML builtin definition 位于 system prompt，不进入 reminder；Native wire 上 MCP 仍是普通 Native tool definition，不引入独立 `NativeMcpSpec` 概念；
- MCP 连接、刷新、工具 discovery、OAuth 和 client teardown 由共享 MCP connection owner 管理，但 definition catalog 与注册槽按 Agent command protocol 分开；
- Native provider options 可承载 server-side tool search/reference；
- XML definition 只进入 XML catalog，Native definition 只进入 Native specs。

同一个 discovered MCP capability 可以由两种 adapter 复用，但不能从一个 definition 自动生成另一个 definition。数组、对象、union 等 XML 无法忠实表达的参数在 XML discovery/prepare 阶段明确失败，不能转成含糊字符串或静默隐藏。

Provider-native built-in tools（web search、computer use 等）只属于 Native Toolset，且可以没有本地 capability；它们通过独立 result normalization 汇入语义事件。这类工具绝不能伪装成 XML `BaseTool`。

## 10. Deferred 与 tool search

当前 deferred 逻辑必须拆到协议 catalog：

- XML：`XmlToolCatalog` 实现真正的 withhold/reveal 和 prompt menu；
- Native：`NativeToolCatalog` 实现 stable specs、description split 或 server defer；
- `SearchTools` capability 可以复用，但其 XML/Native definition 分别注册；
- `defer_loading`、tool references、OpenAI Responses tool search 仅存在于 Native；
- revealed state 可以继续作为协议无关的 durable 名称集合，但 wire 投影由各 catalog 独立解释。

`describe_deferred()` 必须读取当前协议 definition 的 description，不能回退到 capability 上的另一个协议 schema。

## 11. Ownership 与生命周期

Toolset 的组合节点是不可变 definition provider；有资源的 dynamic Toolset 参与 Agent run 生命周期：

```text
Engine owns shared provider services and Agent cleanup
  -> Agent run activates its per-run Toolset view
    -> each model step refreshes only per-step dynamic views
      -> run exit releases inner Toolset resources
```

静态 Product Toolset 无关闭成本。`Role.bind_run_context()` 驱动 `start_run()` / `end_run()`，`ContextProvider.prepare()` 在生成 prompt/spec 之前驱动 `prepare_run_step()`；退出时先解绑动态 capability，再关闭 inner 资源。MCP connection owner 与这套动态刷新独立，清理/替换 declared Toolset 时跳过 MCP category，因此 MCP 热加载 catalog 不会被 per-step refresh 清空。

Engine 最终拥有 Agent cleanup；XML/Native MCP、sandbox/LSP/browser 等长寿命资源仍必须支持并发幂等关闭、waiter cancellation 隔离和失败可重试，与 `EngineServicesLease` 的语义一致。

## 12. 禁止状态

以下模式由架构测试永久禁止：

- 一个 definition 同时暴露 XML 与 Native schema；
- `BaseTool`/`ToolCapability` 同时存在 `tool_schema` 与 `native_schema`；
- `FunctionToolset` 自动生成两种协议；
- `PresentedTool` 同时重写两种 schema；
- `ToolCatalog` 同时提供 XML catalog 与 Native specs；
- XML Toolset 与 Native Toolset 组合或共同注入一个 Agent；
- Native-only definition 出现在 XML prompt catalog；
- XML-only definition 出现在 provider `tools=`；
- MCP discovery 从一个 definition 自动双投影到 XML 与 Native；
- 错误协议被静默跳过；
- 在函数体内 import 以规避本次拆分产生的循环依赖。

## 13. 实现状态与后续边界

已完成：

- `CommandProtocol`、两种 schema DTO、名义 definition 与名义 Toolset；
- `filter`、`prefix`、`rename`、`prepared`、类型化 `with_approval`、`with_instructions`、`combine` 不可变组合代数；
- session-static instructions 的 SP 注入，以及 dynamic per-run/per-step instructions 的 request-only SR 注入；
- wrapper/combined view 对 per-run、per-step 与 async context lifecycle 的传播；
- `XmlDynamicToolset[DepsT]` / `NativeDynamicToolset[DepsT]` 的 per-run、per-step factory；
- 单次 readiness snapshot、跨 Toolset 名称冲突与重复 ID fail-fast；
- `BoundTool`、XML/Native 独立 catalog、Product 显式双协议 registration；
- `XmlFunctionToolset` / `NativeFunctionToolset`；
- MCP 共享连接 owner、显式 XML/Native definition adapter 与独立 catalog 生命周期；
- `ToolsetIdentity(id, version, protocol)`、有序 durable manifest、fork 继承与 resume fail-closed 校验；
- 删除 capability 双 schema API、自动双投影 wrapper 与含糊的旧 `FunctionToolset`；
- 动态生命周期、协议隔离、MCP catalog 保留和统一执行流水线回归测试。

剩余项：

- provider-native server tools 的一等 Native Toolset；
- wheel 安装后的 facade/typing API 测试，以及 MCP/provider cassette 矩阵。

后续每一步仍必须保持所有生产 import 在模块顶部。若出现循环依赖，回到 definition/capability 分层或在 `contracts/ports` 抽窄 Protocol，不允许局部 import。

## 14. 验收标准

- Pyright 能在不运行代码的情况下拒绝 `xml.combine(native)`；
- Runtime 对未做静态检查的错误组合同样 fail-fast；
- Agent 构造时验证 command protocol 与所有 Toolset；
- 新会话持久化有序 Toolset manifest，fork 原样继承，已知 resume mismatch fail-closed，旧日志缺字段单向兼容；
- 每个 readiness 阶段只物化一次 Toolset definition，并从同一 snapshot 校验、解析和绑定；
- XML catalog 与 Native specs 的交集只可能来自两次显式注册，不可能来自自动投影；
- XML structured parameter 在注册阶段失败并给出具体参数名；
- XML 与 Native MCP 生命周期均由 Engine/Agent 统一关闭；
- 动态 Toolset refresh 不删除 MCP category，inner context enter/exit 严格成对；
- 静态 Toolset instructions 不进入 SR，动态 inner instructions 不进入 SP 或历史；
- approval policy 可读取类型化 run deps、definition 与调用参数，但不能脱离中央 Permission gate 执行审批；
- 两协议的调用都通过同一 Permission/effect/snapshot/settlement/audit 流水线；
- 非 `ztest/` 局部 import 守卫维持零基线；
- 删除所有双协议 schema 方法和 wrapper 后，parser/executor/roles/architecture 回归通过。
