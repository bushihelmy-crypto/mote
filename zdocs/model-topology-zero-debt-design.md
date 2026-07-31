# Model Topology：十年零负债实施规格

## 1. 状态与决策

本设计定义 Mote 唯一的模型运行时语义。实施完成后，Runtime 不再接触 Product
模型配置或 Product 编译产物，而只通过 Product 注入的 contracts acquire port 获取
application lease 及其 Runtime-owned model generation lease。

核心决策：

1. Product 输入使用显式 discriminated union，不靠字段默认值猜测模式。
2. compiler 在 Product 内同时产出公开拓扑与私密 credential bindings；二者不可分割，
   但该 Product-owned 产物绝不跨入 Runtime。
3. transport、能力、context window 和 credential slot 全部在 Product 编译期确定。
4. Runtime 的唯一模型事实源是 active generation handle，不是 `Config.models`。
5. 子 Agent 只能选择已编译 route，不允许 ephemeral endpoint 逃生通道。
6. Product/Application 层的 `AtomicApplicationComposition` 是 active application
   generation 的唯一 owner；Runtime model composition 是 generation resource，不另设 current
   pointer，因而不存在 application、Engine 与 gateway 多个权威。

最终数据流：

```text
layered Product input
        |
        v
ShortcutModelsConfig | ExplicitModelsConfig
        |
        v
build_or_reuse_model_generation()
        |
        +-- reuse key equal --> retain SharedRuntimeCompositionHandle
        |
        +-- reuse key changed --> CompiledModelGeneration
                                  ├── ModelTopology
                                  └── CredentialBindingSpec
                                       | build adapters + contracts ports
                                       v
                                  RuntimeCompositionCandidate
        |
        v Product application composition
ApplicationCompositionCandidate
        |
        v
AtomicApplicationComposition -> ApplicationLease -> RuntimeCompositionLease
```

## 2. 当前根因与完整迁移范围

当前 `ModelsConfig` 同时充当：

- Product 用户语法：`default`、`tasks`、secret/helper、provider 配置。
- Runtime IR：`endpoints`、`failover_groups`、`routes`、recovery profiles。

因此同一个对象存在两种解释，双轨分散在：

- `runtime/models/failover/snapshot.py` 的 legacy singleton group 分支。
- `product/models/gateway.py` 的 `default/tasks` credential binding 分支。
- `runtime/agent/components/cognition.py` 的两套 task route map。
- `runtime/agent/role.py` 对 `models.default` 的读取。
- `runtime/agent/components/context.py` 对默认模型名的读取。
- `runtime/models/clients/context.py` 对 Product 默认 LLM 配置的读取。
- 子 Agent 通过复制并修改 `models.default` 选择模型的路径。
- 首次启动、reload 与直接构造测试对象之间不同的装配路径。

迁移范围是整个 `runtime/`，不是仅 `runtime/models/failover/`。最终
`runtime/**` 不得 import Product 模型输入类型，也不得读取 `ModelsConfig`。

## 3. 分层不变量

### 3.1 contracts

`contracts/model/` 定义以下纯数据与窄端口：

- `ModelTopology` 及 endpoint/group/route/recovery 类型。
- 类型化 `RouteId`。
- `GenerationId` 与 topology revision。
- `SourceRevision`、`ReloadSequence`、activation result 与 shutdown report 数据契约。
- Runtime 需要的公开 `DefaultModelMetadata`。
- 不含 Product/YAML 语义的不可变 `RuntimeRoleConfigView`；它只承载 Runtime 已验证、可直接消费的
  Role 运行配置。
- generation、gateway、route policy 的 Protocol。
- Runtime composition candidate/lease 所需的公开数据与窄端口。

contracts 不读取 YAML、不解析环境变量、不解析 provider、不持有 secret、不依赖
Product 或 Runtime。

### 3.2 Product

Product 独占：

- 配置输入 union 与 layered config。
- shortcut 展开。
- provider/transport 解析。
- model capability catalog。
- credential source 解析与 binding spec 构造。
- `CompiledModelGeneration` 的编译，以及 Runtime candidate 的完整装配。
- 将 Product 编译产物装配为只含 contracts 数据和端口实现的
  `RuntimeCompositionCandidate`。
- 将同一 layered application config 的 Runtime Role 字段编译为 canonical
  `RuntimeRoleConfigView`；UI/MCP/skills 等 Product 配置只进入 Product-owned application
  resources，不进入 Runtime model candidate。
- endpoint adapter 实现。

### 3.3 Runtime

Runtime 独占：

- 通过 contracts-owned `ApplicationCompositionPort` 获取固定 application lease，并从中派生
  Runtime model lease；Runtime 不拥有 application current pointer。
- planner、gateway、failover 和调用 journal。
- generation lease 与旧 generation drain。
- 使用已经完整确定的 endpoint capability、transport 和 route policy。

Runtime 不得接收 `CompiledModelGeneration` 或 `CredentialBindingSpec`，不得调用
`resolve_api_type`、model profile/capability catalog、credential source resolver，也不得
从 model 名称推断任何能力。

### 3.4 Kernel

Kernel 只依赖推理端口、请求需求和模型调用结果；不看到 topology、provider、secret
或 Product 配置。

## 4. Product 输入：用类型决定模式

模式判定不得依据 Pydantic 填充默认值后的字段内容，也不得只检查 `endpoints`。
采用显式 discriminated union：

```python
ProductModelsConfig = Annotated[
    ShortcutModelsConfig | ExplicitModelsConfig,
    Field(discriminator="mode"),
]

class ShortcutModelsConfig(...):
    mode: Literal["shortcut"] = "shortcut"
    default: ProductEndpointInput
    tasks: dict[str, ProductEndpointInput]
    recovery_defaults: ProductRecoveryInput

class ExplicitModelsConfig(...):
    mode: Literal["explicit"]
    endpoints: dict[str, ProductEndpointInput]
    credential_pools: dict[str, ProductCredentialPoolInput]
    failover_groups: dict[str, ProductFailoverGroupInput]
    routes: ProductRoutesInput
    recovery_profiles: dict[str, ProductRecoveryInput]
```

这些输入类型位于 `product/config/model/`，不是 `contracts/config/`。现有
`contracts/config/model/ModelsConfig` 在迁移完成后删除；contracts 只保留 canonical
topology 和跨层端口。

规则：

- `mode` 是唯一判据。
- 新配置必须显式写 `mode`；默认配置模板写 `mode: shortcut`。
- 当前 `1.1.x` 配置没有 mode。`1.2.0` 引入新 union：无 mode 仅当字段全集属于 shortcut
  schema 时按 shortcut 读取并发出带 provenance 的 deprecation warning；出现任一
  explicit-only 字段则拒绝并要求显式 `mode: explicit`，不得猜测。
- `1.3.0` 删除无 mode 推断，任何缺失 mode 的配置在 Product loader 失败。该兼容逻辑只存在
  Product 输入边界，不能进入 compiler 或 Runtime。
- shortcut 中出现任何 explicit-only 字段即校验失败。
- explicit 中出现 `default` 或 `tasks` 即校验失败。
- 显式字段的空 `{}` 仍算显式配置；不得因此退回 shortcut。
- explicit 模式必须完整，空 endpoints/groups/routes 直接失败，不能自动补齐。

这样 `recovery_profiles` 的默认 `default` 不再影响模式判定，也不会发生“提供 routes
但漏 endpoints 后被静默覆盖”的情况。

## 5. 编译产物与 secret 数据通路

Product compiler 的唯一产物：

```python
@dataclass(frozen=True, slots=True)
class CompiledModelGeneration:
    topology: ModelTopology
    credential_bindings: CredentialBindingSpec
    route_policy: ModelRoutePolicySpec
    default_model: DefaultModelMetadata
    credential_epoch: CredentialEpoch
```

可复用性不由 topology revision 单独决定。Product-owned、非敏感值对象固定为：

```python
@dataclass(frozen=True, slots=True)
class ModelGenerationReuseKey:
    topology_revision: TopologyRevision
    credential_epoch: CredentialEpoch
    provider_catalog_revision: ProviderCatalogRevision
    adapter_factory_revision: AdapterFactoryRevision
```

`credential_epoch` 是所有 credential source identity/epoch 的 canonical aggregate，不包含、哈希
或截断 secret material。source 无法在不读取 material 的情况下提供稳定 epoch 时必须声明
`NON_REUSABLE`，builder 为每次 reload 分配新 epoch；宁可重建，也不能错误复用旧 credential。
secret helper 只有实现独立、非敏感 revision protocol 时才可复用，否则默认 NON_REUSABLE。
source epoch contract 要求：API key/OAuth client secret/helper output 任何可能变化前必须先改变
epoch；违反该契约是 credential source 实现错误。provider catalog 和 adapter factory 必须各自
暴露 immutable content revision，缺失 revision 时编译失败，不能以类名、包版本猜测或默认
复用。OAuth access-token 在同一 handle 内的受控 refresh 不改变 generation key；其 source/client
credential 或 refresh implementation revision 变化必须通过 credential/adapter revision 体现。

这是 Product-owned、进程内、不可序列化的中间产物。Runtime API 不得引用此类型。
Product composition 随后将 topology 转成 Runtime 索引、将 bindings 转成
`ModelEndpointResolver` 等 contracts 端口实现，并构造：

```python
@dataclass(slots=True)
class RuntimeCompositionCandidate:
    identity: RuntimeGenerationIdentity
    planner: ModelPlanner
    endpoint_resolver: ModelEndpointResolver
    route_policy: ModelRoutePolicy
    default_model: DefaultModelMetadata
```

该 candidate 只包含 contracts 数据、Runtime-owned 对象和 contracts 端口；它是
Product/Runtime 的唯一模型交接物。Product 将它构造成 ref-counted、不可变的
`SharedRuntimeCompositionHandle`，再装入 Product-owned `ApplicationCompositionCandidate`；
Runtime candidate 本身不持有 source sequence、UI/MCP/skills 或 Product Role config。

candidate 中的成员是尚未发布的 model generation 资源。application activation 使用它构造
不可变 `RuntimeCompositionGeneration`；不得把能自行读取 active pointer 的全局 gateway 放入
candidate。对调用方暴露的 gateway 只能来自 generation lease，见第 11 节。

### 5.1 ModelTopology

完全公开、不可变、可使用版本化 canonical codec 序列化，可以进入日志、诊断和
revision 计算。它包含：

- 完整 endpoint identity、transport、capabilities、context window。
- governance domain、region、pricing class。
- secret-opaque credential slot IDs。
- failover groups 与 recovery profiles。
- 类型化 route bindings。

不得包含 API key、OAuth token、secret ref 的解析值或可反推出 secret 的内容。

### 5.2 CredentialBindingSpec 与 secret 生命周期

由 Product compiler 与 secret resolution 阶段共同构造，包含：

- endpoint ID 与 slot ID 的精确绑定。
- `SecretHandle`；binding 不直接持有已解析的 credential material。
- OAuth prepare/refresh capability。
- provider adapter factory 所需的非公开绑定上下文。

不允许“opaque handle 或 material”这种开放联合。统一协议为 Product-owned：

```python
class SecretHandle(Protocol):
    @property
    def identity(self) -> SecretIdentity: ...  # 非敏感、稳定、不可反推 material
    @property
    def epoch(self) -> CredentialEpoch: ...
    async def acquire(self) -> CredentialLease: ...
    async def aclose(self) -> None: ...

class CredentialLease(Protocol):
    async def resolve(self) -> CredentialMaterial: ...
    async def refresh(self) -> CredentialMaterial: ...
    async def release(self) -> None: ...

class CredentialMaterial(Protocol):
    def read_for_wire(self, access: CredentialWireAccess) -> str | bytes: ...
```

生命周期约束：

- `SecretHandle` 由一个 Product generation 独占；不能跨 generation 复用可变 handle。
- `CredentialLease` 是一次调用或一次受控 refresh 的短生命周期能力。
- `resolve/refresh` 只能由 Product endpoint adapter 在已取得 Runtime generation lease
  的调用内执行。
- `release()` 与 `aclose()` 幂等；aclose 后 acquire 必须失败。
- `CredentialMaterial` 是短生命周期 redacted wrapper，不承诺 Python 进程内不可复制或安全
  擦除。
- wrapper 禁止 Pydantic/JSON/pickle；`repr` 与 `str` 始终返回固定脱敏值，不能包含长度、
  前后缀或其他可关联 material 的信息。
- `CredentialWireAccess` 由 Product adapter factory 私有签发并绑定 endpoint/slot；只有
  对应 Product endpoint adapter 能把它传给 `read_for_wire()`。其他调用、错误 access 或
  release 后读取必须失败。该窄口无法阻止恶意进程内代码，但能形成可扫描、可测试的唯一
  正常读取路径。
- wire value 不得缓存进
  topology、adapter 配置快照、journal、event、exception 或 telemetry。
- provider SDK、HTTP client 或 Python `str` 可能产生框架无法控制和可靠清零的内存副本，
  这是本安全模型明确接受的语言/第三方边界。
- `release()` 清除 wrapper 与框架所持引用并使后续读取失败，但不宣称擦除 SDK、HTTP
  stack 或 Python runtime 已产生的全部副本。
- refresh 串行化语义由 handle 实现，不能产生并发 token 覆盖。
- Runtime 只持有 `ModelEndpointResolver` 端口；resolver 关闭时由 Product 实现级联关闭
  handles。Runtime 不看到 SecretHandle/CredentialLease 类型。

`CredentialBindingSpec` 必须：

- 不实现通用 JSON/Pydantic serialization。
- `repr` 不暴露 material，日志装饰器排除其访问器。
- 不进入 topology revision、session、checkpoint 或 telemetry。
- 由 Product generation 生命周期拥有；装配后的 Product resolver 接管所有权，旧
  Runtime generation drain 后通过 resolver `aclose()` 确定性释放。

compiler 不会先“剥离并丢弃”secret。layered loader 解析 trusted source 后，将 secret
作为受控输入交给唯一 compiler；compiler 同时产出 topology 与 bindings。后续
resolver 不再读取原始 Product config。

### 5.3 一致性约束

Product 在构造 `CompiledModelGeneration` 及 Runtime candidate 前后分别验证：

- topology endpoint 集合与 bindings endpoint 集合完全一致。
- 每个启用 endpoint 的 topology slot 集合与 binding slot 集合完全一致。
- 不允许多余 binding、缺失 binding 或未绑定 slot。
- topology、bindings、route policy 和 default metadata 引用同一 topology revision；
  Runtime candidate 的所有成员共享同一个 `RuntimeGenerationIdentity`。

## 6. 类型化 Route ID 与唯一性

Runtime route key 使用 tagged value，而不是扁平字符串约定：

contracts 中定义不可互换的冻结值对象：

```python
class DefaultRoute(FrozenModel):
    kind: Literal["default"] = "default"

class TaskRoute(FrozenModel):
    kind: Literal["task"] = "task"
    name: NonEmptyRouteName

class SemanticRoute(FrozenModel):
    kind: Literal["semantic"] = "semantic"
    name: NonEmptyRouteName

RouteId = Annotated[
    DefaultRoute | TaskRoute | SemanticRoute,
    Field(discriminator="kind"),
]
```

值对象必须 immutable、可比较、可 hash，因此可安全作为 Runtime dict key。拓扑的
Pydantic/JSON 结构使用 tagged object；持久化与外部 wire 边界统一通过唯一 codec 编码：

```text
DefaultRoute()                    <-> default
TaskRoute(name="compression")    <-> task:compression
SemanticRoute(name="fast")       <-> semantic:fast
```

route name 禁止 `:`、空白和保留前缀，decoder 拒绝未知/非 canonical 表达。编码后再解码
必须得到同类型同值对象。

“每条 route 的唯一 group”表示：每个 `RouteId` 必须且只能绑定一个 group。
不同 route 可以有意共享同一个 group；不要求为每个 task 复制 group。

route、endpoint、group、profile 分属不同类型命名空间。字符串 wire form 只用于稳定
序列化和诊断，内部 API 不接受裸字符串互换。具体边界：

- invocation、gateway、planner、routing policy、Runtime state 内部字段使用 `RouteId`。
- Runtime 公共 Python API 只接受 `RouteId`，不隐式接收 `str`。
- JSON/session/journal/event payload 保存稳定 wire string，并在 producer/consumer codec
  边界显式 encode/decode。
- 日志可以记录 wire string，但不得把它回流为内部 route key。
- CLI/YAML 接收字符串后必须在 Product 输入边界解析为 `RouteId`。
- 历史持久化 upcaster 只存在 session migration 边界，不能放进 RouteId decoder。

## 7. Shortcut 编译规则

shortcut compiler 生成完整 generation：

1. default endpoint ID 为 `endpoint:default`。
2. task endpoint ID 为 `endpoint:task:<task>`。
3. 默认生成 singleton group；编译器允许相同的完全解析 endpoint 输入按确定性规则
   去重并共享 group，但不能根据模型名猜测共享。
4. `DefaultRoute()` 绑定 default group。
5. `TaskRoute(task)` 绑定对应 task group。
6. transport、credential inheritance、OAuth、capabilities 和 context window 在编译期
   完全解析，输出不存在“继承中”或“自动识别中”状态。
7. secret 只进入 bindings，不进入 topology。
8. 编译结果经过与 explicit 模式相同的 canonical validator。

### 7.1 Compression profile

`TaskRoute("compression")` 必须绑定 `max_request_transforms == 0` 的 profile。
compiler 从 recovery defaults 派生专用 profile，例如
`profile:task:compression`，显式覆盖 transform budget。

该规则属于 Product 编译政策，不得在 Runtime 根据 route 字符串打补丁。

## 8. Transport 与能力解析

Product compiler 是唯一解释 provider/model 语义的位置：

- 解析 wire transport。
- 读取 capability catalog。
- 确定 tools、native schema、server web search、vision、PDF、native tool search。
- 确定 context window。
- 校验显式 capability override 与 transport/provider 的兼容性。

Runtime snapshot 只做已类型化结构到高效不可变索引的转换和 revision 验证，不再推断。

### 8.1 未知模型政策

未知模型不能采用“看起来兼容”的乐观默认值：

- explicit 模式下，未知模型必须显式填写全部 Runtime 所需 capability 与
  `context_tokens`；缺任意必需项即编译失败。
- shortcut 模式下，未知模型同样失败，并提示改用 explicit 模式补齐能力。
- transport 不能从 model 名推断；必须由 endpoint/provider 输入或 provider catalog
  唯一确定，否则失败。
- 保守的 `False/0` 不作为兜底，因为它会把配置错误伪装成运行时能力不足。

## 9. Compiler 与 model generation builder API

从 Product 配置进入模型语义的唯一公开入口是：

```python
async def build_or_reuse_model_generation(
    source: ProductModelsConfig,
    *,
    provider_catalog: ProductProviderCatalog,
    adapter_factories: ProductAdapterFactoryCatalog,
    credential_sources: ProductCredentialSourceCatalog,
    current: ReusableModelGeneration | None,
) -> ModelGenerationBuildResult:
    ...
```

它内部由不可绕过的 public-plan、reuse decision、materialization 三阶段组成；内部 helper 不得
成为 composition root 的第二入口：

- 不读取环境变量、文件、全局 registry 或进程单例。
- loader 在调用前完成 layered config 与 trusted-source 选择；传入的 credential source catalog
  首先只暴露 identity/epoch metadata 和延迟的 handle factory，不预先创建 SecretHandle。
- public-plan 阶段完整解析 topology、policy、metadata 和四元 reuse key；provider/adapter
  catalog revision 均显式注入并进入 key。
- key 与 current 完全相等时，原子 retain current `SharedRuntimeCompositionHandle` 并返回
  `ReusedModelGeneration`；此路径禁止调用 handle factory、compiler binding 或 readiness。
- key 不同或没有 current 时，才创建 SecretHandle catalog，完成 binding、resolver、candidate
  与 readiness，返回 `NewModelGeneration`。
- compiler 不调用 `SecretHandle.acquire()`，不解析 credential material。
- materialization 任一步失败或 candidate 被 latest-request-wins 淘汰时，builder 尽力关闭本次
  新建的全部 handles/resources并聚合错误。即使实现缺陷导致 handles 在 reuse decision 前已
  创建，reuse 分支也必须先关闭它们才能返回，测试把这种路径作为故障注入门禁。
- 不修改输入。
- 相同公开输入和 catalog revision 产生字节相同的 topology、route policy spec、default
  metadata 和 topology revision。
- `CompiledModelGeneration` 整体不承诺相等或 identity 确定性，因为 credential handle、
  credential epoch 和后续 Runtime generation identity 具有生命周期语义。
- secret material 变化不改变 topology revision；credential epoch 变化使装配阶段创建
  新的不可预测 Runtime generation identity。
- 所有错误包含 Product 输入路径和违反的约束。
- Product resolver 只消费 `CredentialBindingSpec`，不再解释 source config。

内部 `compile_model_generation(public_plan, secret_handles)` 仍是产生
`CompiledModelGeneration` 的唯一函数，但只有 builder 在 key 判定为不复用后可调用；生产
composition root 不得直接调用它。

## 10. Revision 与 generation identity

二者语义不同：

- `topology_revision`：公开物理/策略拓扑的内容地址。
- `RuntimeGenerationIdentity`：一次完整可安装 Runtime composition 的身份，包括
  credential binding epoch，但不通过 material 哈希泄露 secret。

revision 使用版本化 canonical serialization：

```text
sha256("mote-model-topology-v1\0" + canonical_bytes)
```

canonical codec 必须规定字段顺序、集合排序、枚举 wire value、缺省字段表达和数字编码。
未来格式变化使用 `v2` 前缀；不能因 Python/Pydantic 默认 dump 行为变化产生无意义漂移。

Runtime generation ID 在 Product composition 构造 candidate 时生成，不是 compiler
确定性输出的一部分。它使用不可预测 ID，并同时记录 topology revision 与 credential source
revision/epoch；不得包含 secret hash、key 前缀或 token 内容。

## 11. AtomicApplicationComposition：唯一事实源

最终态中，Product/Application 层的 `AtomicApplicationComposition` 是 active application
generation 的唯一 owner。Engine 只拥有这个容器；Runtime、gateway 和现有
`AtomicModelRuntime` 都不得维护 current pointer。迁移期 reachability 规则见第 16 节；阶段 E
完成后 `AtomicModelRuntime` 被删除而不是与 application container 并存。

Product-owned application generation 包含：

```text
ApplicationGeneration
├── application_generation_id / source_revision / reload_sequence
├── SharedRuntimeCompositionHandle
├── RuntimeRoleConfigView          # contracts-owned canonical view
├── ProductRoleConfigView          # Product-owned，不跨入 Runtime
└── Product resources              # UI/MCP/skills/integrations 等
```

`SharedRuntimeCompositionHandle` 指向不可变 Runtime model generation：

```text
RuntimeCompositionGeneration
├── identity
│   ├── runtime_generation_id
│   └── topology_revision
├── ModelGateway
├── ModelRoutePolicy
└── DefaultModelMetadata
```

Role 每个 turn 开始只 acquire 一次 Runtime-facing `ApplicationLease`，该 turn 的 Runtime role
view 与模型 handle 固定；Product services 通过同一 application generation 的 Product-owned
view 使用 Product resources，但这些属性不出现在 Runtime-facing lease 上。所有 accessor 禁止
重新读取 current pointer。模型调用从 application lease 派生 `RuntimeCompositionLease`；
gateway、route policy、default metadata 不得分别读取 current pointer。一次推理从 route
selection 到 journal attribution 使用同一 model lease identity。

- `Role` 获取默认模型显示信息时，从一次 composition lease 读取
  `DefaultModelMetadata`。
- `ContextProvider` 从同一 lease 的 `ModelRoutePolicy` 选择类型化 route。
- 工具提示与诊断通过只读 metadata/diagnostics port 获取信息。
- `runtime/models/clients/context.py` 不再从 `Config.models.default` 构造 client。
- Runtime 测试直接构造 RuntimeComposition fixture，不构造 Product `ModelsConfig`。

整个 `runtime/` 必须迁移，不能留下 `role.config.models.default` 作为旁路。

### 11.1 Lease-bound 调用 API

单阶段调用的唯一合法形态是一次 context-managed lease。现有 `resolve() -> infer()` 两阶段
接口则必须显式返回持有该 generation lease 的 `InferenceTargetLease`：

```python
target = await inference.resolve(request)
try:
    result = await inference.infer(target, request)
finally:
    await inference.release(target)
```

`RuntimeCompositionLease` 是异步 context manager。进入成功后，它对一个不可变
`RuntimeCompositionGeneration` 持有引用计数式生命周期权利；退出时释放一次。lease 上的
`gateway` 是绑定该 generation 的 `GenerationBoundModelGateway`，其 planner、resolver 和
route index 都来自同一 generation，任何方法都不得调用全局 application acquire port 或读取
current pointer。

硬约束：

- route selection、profile 查询、endpoint resolve、provider invocation、failover 和
  journal attribution 必须发生在同一个未释放 lease 内。
- `ModelRoute`、planner result 或 adapter 不得保存全局 gateway/acquire port；它们只持有
  generation-bound 对象。
- 跨异步阶段保存的 `InferenceTarget` 必须包含不可伪造的 generation fencing identity，
  至少为 `runtime_generation_id + topology_revision`，并由 bound gateway 在 execute 前与
  自身 identity 精确比较；跨 generation 使用立即失败，不得重路由到 current generation。
- lease 退出或 generation 被强制关闭后，lease、bound gateway、target 和其 adapter 的
  后续公开调用均以确定的 `LeaseReleasedError` 失败。
- journal/event 的模型调用归因同时记录稳定编码的 `RouteId`、
  `runtime_generation_id` 与 `topology_revision`；不得在记录时重新 acquire。
- 面向 Kernel 的窄 inference port 可以封装上述 context manager，但一次端口调用内部仍只
  acquire 一次，不能把裸 lease 或 bound gateway 泄漏给 Kernel。
- model lease 是 application lease 的 child lease。正常 turn 必须先结束全部 target/model
  lease，再退出 application lease；若异常次序退出，application lease 保留 generation 引用并
  请求取消 child，直到 child finally 释放，不能提前关闭 model/Product resources。

### 11.2 InferenceTargetLease 生命周期

`InferenceTargetLease` 是 opaque、move-only、单次消费的 capability，不是可长期保存的普通
DTO。`resolve()` 原子完成 composition acquire、route selection、planning 和 target registry
登记；target 保存 target ID、generation fencing identity、创建时间和 bound gateway，不暴露
底层 composition lease。

状态机为 `READY -> ACTIVE -> RELEASED`，另有清理产生的 `EXPIRED`：

- 一个 target 只允许一次 `infer()`，不允许并发或重复调用；第二次或并发调用以
  `TargetAlreadyConsumedError` 失败。
- `infer()` 以原子状态转换取得 target，成功、异常、取消或超时都在 infer owner 的内部
  `finally` 释放底层 model lease；这是 `ACTIVE` 状态唯一允许实际释放 lease 的路径。
- provider/failover retry 只能在这一次 `infer()` 内由 bound gateway 执行并复用同一
  generation lease。跨 `infer()` 重试必须重新 `resolve()`，不得复活旧 target。
- `READY + release` 原子转为 `RELEASED` 并直接释放 lease；重复/并发 release 幂等等待同一个
  completion future。
- `ACTIVE + release` 不得释放 lease。它只向 infer owner 请求取消并等待同一个 completion
  future；若调用方选择 non-waiting API，则返回 `TargetActiveError`。provider 不响应取消时
  target 保持 `ACTIVE`，进入 drain/shutdown timeout 诊断，绝不能抢先 close resolver。
- `READY` target 的固定 TTL 为 5 分钟；后台 reaper 只扫描 READY，到期后转为 `EXPIRED` 并
  释放 lease，永不触碰 ACTIVE。`ACTIVE` 只由 infer timeout/cancellation/finally 收敛。
- registry 硬上限为 1024 个未释放 target；达到上限时 `resolve()` 背压，超过调用 deadline
  后以 `TargetCapacityError` 失败，不能无界占用 retired generation。
- READY release/reaper 竞争时只有胜者释放；ACTIVE 的所有 concurrent release 共享 infer
  completion future，最终只有 infer-finally 释放。`RELEASED/EXPIRED` target 的 infer、
  profile、adapter 操作确定失败。
- target registry 暴露 READY/ACTIVE 数量、年龄、generation identity 和最老 target ID；不得
  暴露请求正文或 credential。

任何自行 acquire current generation 的 `ModelGateway.execute()` 和任何由
`ModelRoute` 回指全局 gateway 的设计都必须删除，不能作为兼容入口保留。

## 12. 原子 reload 与并发语义

Engine/Application 拥有一个 Product-layer `AtomicApplicationComposition`；它内部的唯一
pointer 拥有 active application generation。Role 只持有 contracts acquire port，不持有
generation、route policy handle 或 Product 模型配置副本。

reload 流程：

1. composition coordinator 在接收 reload 时分配进程内严格单调的 `ReloadSequence`，记录
   配置源的稳定 `SourceRevision`，并记录 `ExpectedEmpty` 或当时的 active generation ID。
2. 在 current pointer 之外加载 application input，并先只解析公开模型 plan、credential source
   identity/epoch、provider catalog revision 和 adapter factory revision。
3. 计算 `ModelGenerationReuseKey`。只有四项与 current key 完全相同才 retain 当前
   `SharedRuntimeCompositionHandle`，且此路径不得创建 SecretHandle。
4. key 不同才创建 handles、编译 bindings、构造 Runtime model candidate；Product 验证
   topology/bindings slot 完全一致，构造 resolver adapters，并完成本地 readiness checks。
5. Product 同时构造 `RuntimeRoleConfigView` 与 UI/MCP/skills 等 Product resources，形成完整
   `ApplicationCompositionCandidate`。
6. 使用唯一 activation token 调用
   `AtomicApplicationComposition.activate(candidate, token, expected)`，其中 `expected` 是
   `ExpectedEmpty | ExpectedActive(application_generation_id)`，
   Product 在一个临界区交换唯一 pointer；Runtime 没有第二次 activation。
7. 新 turn 获取新 application lease；已取得 lease 的 turn 继续使用旧 application generation。
8. 旧 application generation 等待 application/child model lease 归零后关闭 Product resources
   并 release model handle；model handle 总引用归零时才关闭 resolver、OAuth/client pool 和
   secret handles。
9. drain 超时进入可观测的 degraded 状态，但不能强行把 inflight call 切到新 resolver。

任意 candidate 构造、验证或 readiness 失败：

- 不修改 active generation 的任何成员。
- 不修改 Role 可见 route policy/default metadata。
- 关闭 candidate 已创建资源。
- 上一 generation 完整可用。

### 12.1 Container 启动与关闭状态机

`AtomicApplicationComposition` 的封闭状态机是：

```text
EMPTY --first activate--> ACTIVE --shutdown--> SHUTTING_DOWN --drained--> CLOSED
  |
  +--shutdown-----------------------------------------------------> CLOSED
```

- 新容器始于 `EMPTY`，没有 current pointer。`acquire()` 确定返回
  `ApplicationNotReadyError`，不能返回空 lease 或构造无模型降级对象。
- 首次 activation 必须传类型化 singleton `ExpectedEmpty`；禁止用 `None` 同时表达“期望为空”
  和“未提供 CAS”。首次 accepted request 的 `ReloadSequence` 为 1，容器内部
  `last_committed_reload_sequence` 初值为 0。
- `EMPTY` 首装仍执行 source latest check、readiness、ownership 和 cancellation ledger。
  candidate 失败或提交前取消后保持 `EMPTY`，并按 ownership 规则完整关闭。
- 第一次 pointer swap 原子执行 `EMPTY -> ACTIVE`。`ACTIVE` 下再次传 `ExpectedEmpty` 必须以
  `ExpectedStateMismatchError` 失败；两个并发首次 activate 串行后只能一个成功，另一个关闭
  自己的未提交 candidate。
- `ACTIVE` shutdown 先停止 admission 并转为 `SHUTTING_DOWN`；现有 lease 按 drain 规则收敛。
  timeout 时保持 `SHUTTING_DOWN` 并返回 report，不能谎称 CLOSED；最后 lease/resource 关闭后
  自动转为 `CLOSED`。
- `EMPTY` shutdown 合法、幂等并直接转为 `CLOSED`；对 `SHUTTING_DOWN/CLOSED` 重复 shutdown
  也幂等。
- `SHUTTING_DOWN` 或 `CLOSED` 的 acquire/activate 均以类型化
  `ApplicationShuttingDownError` / `ApplicationClosedError` 确定失败。

启动 composition builder 必须先成功安装初代 application generation，再开放 Engine/CLI/API
admission、健康检查和后台 watcher。初代构造或 activation 失败时启动整体失败并关闭 candidate
与已构造资源；不得对外暴露“无模型但健康”的应用。

### 12.2 Candidate 所有权与 activate 状态机

所有权只有一次、在原子提交点转移：

```text
candidate 构造/readiness/activate 提交前  Product builder owns candidate
activate 原子提交成功                    AtomicApplicationComposition owns resources
activate 失败或提交前取消                 Product builder still owns candidate
旧 application generation drain 完成      Product closes/releases owned resources
```

`ApplicationCompositionCandidate` 是成员不可替换但带私有生命周期状态的 move-only 语义对象，
因此不是 frozen value dataclass。初始为 `NEW`，真正开始一次 activate 尝试后即被消费；成功
变为 `COMMITTED`，失败或提交前取消变为 `REJECTED`。等待 activate 锁期间取消尚未开始尝试，
保持 `NEW` 且仍归 Product。再次安装 `COMMITTED/REJECTED` candidate 必须失败。Python 无法
静态兑现 move-only，因此由私有状态、唯一 activation token 和并发测试强制执行，禁止成员
替换、shallow/deep copy、pickle 和 Pydantic serialization。

调用方先获得唯一 `ActivationToken`，再调用
`activate(candidate, token, expected: ExpectedEmpty | ExpectedActive) -> ActivationReceipt`。
其语义：

- 多个 activate 请求由容器内单一异步锁严格排队；等待锁期间取消不触碰 candidate，调用方
  仍拥有它。首版不采用竞态敏感的 try-lock 拒绝策略。
- 获锁后先完成全部可能失败的提交前检查。提交临界区只做 generation 构造、pointer swap 和
  ownership flag 变更，不含 await，并屏蔽取消。
- 原子 swap 是唯一 ownership transfer 点。swap 前失败/取消绝不安装；Product 在
  `finally` 中对仍由自己拥有的 candidate 执行 `aclose()`。
- activation 状态机在独立的 shielded operation 中运行；一旦进入提交临界区，调用 task 的
  取消不能中断 swap 或 ownership flag 更新。正常返回
  `ActivationReceipt(application_generation_id, source_revision, runtime_generation_id)`。
- Python 取消可能先于 shielded operation 的结果投递给等待者，因此不能承诺调用方总是直接
  收到 receipt。若 `activate()` 抛出 `CancelledError`，调用方必须以 token 调用幂等的
  `activation_result(token)`：返回 committed receipt、明确的 not-committed 结果，或等待仍在
  完成的 shielded operation；不得靠 current pointer 猜测。
- token 只标识一次 activation result，按下述 ledger retention policy 保留，不持有
  generation/lease，也不得成为第二个 generation owner。
- receipt 证明已提交身份，不持有 lease，也不延长旧或新 generation 生命周期。

activation result ledger 的容量不能恢复所有权歧义：

- `PENDING` 不得因 TTL 或容量淘汰。
- `ActivationToken` 是容器签发、可认证且不可伪造的 opaque token。任意 final result 无论是否
  acknowledge 都保留至少 24 小时，之后可删除详细记录，不保留逐 token 永久 tombstone。
- ledger 容量只统计 `PENDING + 24 小时 retention window` 内记录；达到 4096 时对新
  activation 施加背压并最终拒绝。到期 final record 正常清理，遗漏 acknowledge 不能永久
  耗尽 ledger。
- `activation_result(token)` 幂等。对已删除记录的有效 token 返回 `EXPIRED` 和固定
  `CALLER_MUST_NOT_CLOSE` disposition：activation operation 保证 candidate 要么已提交归
  Runtime，要么未提交资源已由 operation 关闭。无效/伪造 token 返回 `UNKNOWN_TOKEN`，不能
  与 expired 混同。
- shutdown 停止接收 activation，等待所有 `PENDING` operation 终结为 committed/rejected；
  rejected 且无人接收的 candidate 由 operation 关闭，然后 ledger 才可完成 shutdown report。
- coordinator 在正常 `finally` 中 best-effort acknowledge 结果以改善诊断，但安全性、清理与
  ownership disposition 不依赖该 finally 被执行。

资源关闭契约：

- readiness 创建的临时 adapter 始终由 Product candidate builder 拥有，并在 readiness
  结束的 `finally` 中关闭；它不进入 candidate。candidate 中只放正式 generation 资源。
- Product builder 对明确返回的未提交 candidate 负责 `aclose()`；取消歧义路径由 activation
  operation 按 ledger 规则收敛。application container 只关闭已提交且 drain 完成的旧
  application generation，双方不得重复拥有同一资源。
- candidate/generation 的 `aclose()` 均幂等，并尝试关闭全部子资源；某个
  adapter/resolver/handle 关闭失败不能阻止其余资源关闭。
- 多个关闭失败聚合为一个类型化 `CompositionCloseError`，保留每个资源的非敏感 identity
  与异常；错误、日志和 telemetry 仍遵守 credential redaction。
- activate 成功后的旧 generation close 失败不回滚新 pointer；容器进入可观测 degraded
  状态并报告聚合错误，因为此时回滚会制造第三种、不可证明的所有权状态。

### 12.3 Reload 顺序、CAS 与 retired generation 预算

`SourceRevision` 是源内容的稳定 revision，用于诊断和去重；跨不同内容不依靠 hash 大小排序。
顺序权威是 coordinator 在 reload 入口分配的 `ReloadSequence`。candidate 必须携带两者，且
activate 在同一提交临界区执行：

- `expected` 必须匹配容器状态：EMPTY 只接受 `ExpectedEmpty`；ACTIVE 只接受 identity 等于
  current application identity 的 `ExpectedActive`，否则返回 CAS conflict。
- candidate sequence 必须同时大于 `last_committed_reload_sequence`、等于 coordinator 的
  `latest_requested_sequence`，且 candidate source revision 必须仍等于 reload source 的当前
  revision。任一条件不满足均返回 `StaleReloadError` 并关闭 candidate。
- latest-request-wins 是硬语义：若 N+1 已被接受但编译/readiness 失败，保持原 active
  generation；随后完成的有效 N 仍必须拒绝，不能用旧请求掩盖最新配置错误。
- CAS conflict 的 coordinator 只可重新读取最新源并分配新 sequence 后重新编译；不得给旧
  candidate 换 expected ID 后强行提交。
- load、compile、readiness 可以并发；提交顺序由上述 CAS/sequence/current-source check
  决定。测试必须覆盖 N 慢/N+1 先提交，以及 slow-valid-N/fast-invalid-N+1。

drain timeout 只告警和改变健康状态，绝不关闭仍有 lease 的 generation。lease 归零事件无论
是否已 timeout 都会触发一次自动关闭。首版硬上限为 2 个尚未关闭的 retired application generations
（active 不计入）；达到上限时拒绝新 reload 并返回 `RetiredGenerationCapacityError`，直到至少
一代 drain/close 完成，不得牺牲 inflight 调用腾位。

诊断必须逐代暴露 generation identity、source revision、retired age、lease 总数、
READY/ACTIVE target 数以及最老 lease/target 的非敏感 identity 和年龄。graceful shutdown：

1. 停止新 inference、resolve、reload 和 activation admission。
2. 立即 expire/release 所有 READY target；取消受 Runtime 管理的 ACTIVE inference，使其
   finally release；外部持有的普通 composition lease 只能等待调用方释放。
3. 最多等待 30 秒 drain，并在 lease 归零时照常关闭每代全部资源。
4. 超时返回类型化 `ShutdownLeaseTimeout` report，列出未释放 generation/lease identity 与
   年龄；不强制关闭其资源。最终是否终止进程由宿主决定，Runtime 不伪造“已安全关闭”。

### 12.4 Readiness v1

首版 readiness 只做本地、无费用、无 provider 请求的可构造性检查：

- topology/binding endpoint 与 slot 一致性。
- provider factory 已注册，transport 与 adapter 类型匹配。
- secret handle 可创建但不调用 `CredentialLease.resolve/refresh`。
- OAuth 配置结构完整，但不刷新 token。
- planner、route policy、default metadata 可以完成本地构造。
- resolver `resolve(endpoint, slot)` 可以返回惰性 adapter；adapter 构造不得联网。

readiness 禁止发送模型请求、探测 endpoint、消耗配额、触发 OAuth refresh 或产生计费。
网络健康由首个真实调用及现有 breaker/failover 语义处理。未来若增加主动探测，必须另立
协议，定义超时、并发、费用、副作用、凭据刷新和失败分类；不能扩展 v1 方法的隐含行为。

### 12.5 Application 配置原子提交

同一份配置文件必须整体生效，因此不采用模型事务成功后再单独发布 Role 配置的 partial-apply
方案，也不把完整 Product config 塞进 Runtime model generation。Product-layer
`AtomicApplicationComposition` 的唯一 pointer 同时发布 model handle、canonical
`RuntimeRoleConfigView`、Product Role/UI/MCP/skills views 与对应 resources。

边界与复用规则：

- Runtime 只看到 contracts-owned `ApplicationLease`、`RuntimeRoleConfigView` 和
  `RuntimeCompositionLease`；不得 import Product config/view/resource 类型。
- 纯 UI 或其他变化只有在完整 `ModelGenerationReuseKey` 相等时才 retain 同一
  `SharedRuntimeCompositionHandle`。topology 相同但 credential epoch、provider catalog revision
  或 adapter factory revision 任一变化都必须创建新 runtime generation；reuse 路径不创建新
  credential handle、resolver 或 client pool。
- 同一配置源中的 MCP/skills 变化属于同一个 application transaction：新资源在 readiness
  完成后随唯一 pointer swap 发布。若存在独立动态 MCP/skills 管理源，必须使用其独立、明确
  的资源事务，不能伪装成模型 generation reload。
- Role 每个 turn acquire 一次 application generation 并固定到 turn 结束；所有 accessor 使用
  该 lease，禁止中途重读 current。model-call lease 从该 application lease 派生，并按 child
  lease 规则先释放。
- ApplicationGeneration 对 shared model handle 持有一个引用；新旧 application generation
  只可在 reuse key 全等时共享 model handle。只有 handle 总引用与全部 model-call leases同时归零，
  Runtime model resources 才 drain/close。

Role-specific mutable state 不进入配置 view。若某项配置需要在提交时执行可能失败的副作用，
必须在 readiness 阶段预构造为 application candidate resource；pointer swap 后不得再有
“第二步发布”。因此任意提交前失败保持整个旧 application generation，提交成功则整个新
generation 可见，不存在 model N+1 / Role/Product config N 的合法状态。

## 13. 子 Agent

子 Agent 只能引用 active generation 中已经存在的 `RouteId`。spawn policy 校验 route
存在、启用并满足治理约束，然后把 route selection 注入子 Agent。

删除：

- 修改 `child_config.models.default.model`。
- 为子 Agent 复制 Product 模型配置。
- ephemeral endpoint request。
- 绕过 route policy 直接按 model 名创建 client。

未来若确需动态 endpoint，必须另立完整设计；本规格不预留半成品扩展口。

## 14. 装配入口

首次启动、CLI、Engine API、managed reload 和 config watcher 必须共享：

```text
load Product layers
  -> parse ProductModelsConfig union
  -> inspect trusted credential source identity/epoch
  -> build_or_reuse_model_generation
  -> retain shared handle OR build RuntimeComposition candidate
  -> build ApplicationCompositionCandidate
  -> atomic application install
```

禁止测试 harness、子 Agent factory 或 reload 路径复制此流程。测试使用同一 composition
builder，或直接使用低层 canonical fixture；不得把未编译 Product config 塞进 Runtime。

## 15. 测试规格

### 15.1 输入模式

- discriminator 唯一决定 shortcut/explicit。
- 空 `{}` 显式字段不会触发模式切换。
- shortcut 携带 explicit 字段失败。
- explicit 携带 `default/tasks` 失败。
- explicit 缺 endpoint/group/route/profile 失败。
- 无 discriminator 的迁移只在 loader compatibility test 中存在，并带删除版本。

### 15.2 Compiler

- shortcut 与等价 explicit 输入产生相同 topology。
- topology 与 binding endpoint/slot 集合完全一致。
- compression 使用 no-transform profile。
- OAuth current/refresh slot 正确。
- 多 key slot 顺序和身份稳定。
- 未知模型缺 capability 失败；完整显式 capability 成功。
- secret 变化不改变 topology revision；装配出的 Runtime generation identity 更新。
- topology 不变但 credential epoch、provider catalog revision 或 adapter factory revision 变化时
  reuse key 改变并创建新 model generation。
- reuse key 完全相等时不调用 SecretHandle factory；readiness 失败、stale candidate 和并发
  reload loser 均关闭本次新建的全部 handles。
- 公开编译产物满足确定性；整个 `CompiledModelGeneration` 不做相等性承诺。
- SecretHandle/Lease redaction、幂等 release/close、close-after-acquire failure 通过测试。
- CredentialMaterial 禁止 serialization/pickle，`repr/str` 脱敏；只有匹配 endpoint/slot 的
  adapter access 可读取 wire value，release 后读取失败。
- 输入对象不被修改；公开 topology/policy/metadata 编译确定性成立。
- mixed mode 和不完整 topology 不会被修复。

### 15.3 Runtime

- Runtime 不消费 `CompiledModelGeneration` 或 `CredentialBindingSpec`，只接收
  `RuntimeCompositionCandidate`。
- Runtime 不调用 transport/capability/credential resolver。
- planner、gateway、Role、ContextProvider 不读取 Product config。
- 类型化 default/task/semantic route 不发生碰撞。
- 多 route 可以显式共享 group。
- RouteId tagged JSON、wire codec、dict key hashing 和 journal/event round trip 通过。
- 单次推理只 acquire 一次；route/profile/target/execute/journal 全部使用同一 lease identity。
- bound gateway 不读取 current pointer；旧 target 不能交给新 generation gateway。
- lease 释放后，lease/gateway/target/adapter 调用确定失败。
- journal 同时记录 RouteId wire value、runtime generation ID 与 topology revision。
- target 正常完成、provider 异常、取消、超时和调用方遗忘五条路径最终都释放 lease。
- target 单次消费、禁止并发/重复 infer；内部 retry 保持原 generation，外部 retry 重新 resolve。
- READY target TTL/reaper、1024 容量背压和 stale/released 确定失败通过 fake-clock 测试。
- READY 外部 release 直接释放；ACTIVE 外部 release 只能取消并等待，provider 不响应取消时
  model lease 始终有效且进入 shutdown timeout report。

### 15.4 Reload

- EMPTY acquire 返回 not-ready；首次 activate 只接受 `ExpectedEmpty` 且 sequence 为 1。
- 首次 candidate 构造失败、activation 失败或取消后保持 EMPTY 并关闭全部资源。
- 两个并发 `ExpectedEmpty` 只有一个提交；另一个 state-mismatch 且关闭自身 candidate。
- EMPTY shutdown 直接 CLOSED 且幂等；CLOSED acquire/activate 确定失败。
- Engine/CLI admission 与健康状态只在初代 receipt 成功后开放，初代失败则启动失败。
- planner、resolver port、route policy、metadata、Runtime generation identity 同时切换。
- candidate 任意阶段失败，旧 generation 完整不变。
- inflight call 使用旧 lease，新 call 使用新 lease。
- drain 后旧 resolver 与 secret handles 关闭一次。
- 并发 reload 串行化或明确拒绝，不产生交叉 generation。
- readiness v1 不发生网络、计费或 OAuth refresh。
- candidate 仅可安装一次；提交前取消不安装，提交后取消可用 activation token 查询 receipt。
- 并发 activate 严格排队，等待期取消不改变 candidate ownership。
- readiness 临时 adapter 必定关闭；candidate/generation close 幂等、尽力关闭全部资源并聚合
  非敏感错误。
- 慢 N/快 N+1 乱序编译只能提交 N+1；旧 expected generation 或旧 reload sequence 被拒绝。
- activation pending 不被淘汰；所有 final result 无论是否 acknowledge 均保留 24 小时后清理，
  有效 token 返回 `EXPIRED/CALLER_MUST_NOT_CLOSE`，伪造 token 返回 `UNKNOWN_TOKEN`。
- drain timeout 不强制关闭；lease 后续归零仍自动关闭。两个 retired application generations 达上限后
  拒绝 reload，释放一代后恢复 admission。
- shutdown 停 admission、清理 READY target、取消受管 ACTIVE inference、等待 30 秒，并对
  外部未释放 lease 返回完整非敏感报告。
- application pointer 同时发布 shared model handle、`RuntimeRoleConfigView` 和 Product
  resources；任一 readiness/提交前故障均保持整代不变。
- slow-valid-N/fast-invalid-N+1 最终保持原 active generation，N 不得随后提交。
- ACTIVE target 的 concurrent release 只请求取消并等待 infer completion；不可响应取消的
  provider 保持 lease 并进入 shutdown timeout report。

### 15.5 Product 集成

- YAML/env/CLI/programmatic/managed 输入共享 compiler。
- 首次启动和 reload 共享 composition builder。
- 子 Agent 只能选择已存在 route。
- Engine、CLI、测试 Product harness 生成一致 generation。
- 纯 UI reload 创建新 application generation 但复用 runtime generation identity、resolver 与
  credential handles，前提是四元 reuse key 完全相同。
- secret epoch/provider catalog/adapter factory revision 单独变化均重建 model handle；并发
  reload 被淘汰的 candidate 不泄漏 handle/resolver。
- 同源 MCP/skills readiness 失败不发布 application generation，旧模型与 Product resources
  整体保持可用。
- 一个 Role turn 内多次 accessor 均看到同一 application generation；下一 turn 才可见 reload。

## 16. 实施顺序

本规格仍处于评审期，不授权开始实现。P0、P1 已关闭，当前 P2 尚待最终签字；mode-aware
merge、canonical codec、输入/历史迁移、运维契约、阶段 0 manifest、逐阶段测试门禁、model
reuse key 与 ASCII-host policy 须经最终开工审查确认，不因本次修订自动开始阶段 0 或 A。

### 16.1 P0 关闭矩阵

| 阻断项 | 规范决策 | 验收门禁 |
| --- | --- | --- |
| 跨层编译产物 | Product 将私密 binding 装配为只含 contracts 端口/Runtime 对象的 candidate；Runtime 不见 Product 类型 | architecture import scan + Runtime candidate tests |
| generation 单一权威 | `AtomicApplicationComposition` 是唯一 current pointer owner；Runtime model generation 无 pointer | 禁止第二 pointer + lease/reload tests |
| RouteId 可实施性 | 内部 tagged/hashable value；持久化边界唯一 wire codec | codec、journal/event round trip、裸字符串扫描 |
| 编译确定性 | 仅公开 topology/policy/metadata 确定；生命周期 identity 不参与 | canonical byte/revision tests |
| secret 生命周期 | Product-owned Handle/Lease；material 为短生命周期 redacted wrapper，明确 Python/SDK 边界 | redaction、唯一 wire-read seam、release/close tests |
| readiness | v1 仅本地构造，不联网、不刷新、不计费 | forbidden-effect tests |
| Product 输入归属 | discriminated union 位于 `product/config/model/`，contracts 仅 canonical 类型 | layering scan + parser tests |
| 同 lease 调用链 | generation-bound gateway；route 到 journal 全程一次 lease，target 带 fencing identity | reload race、stale target、released lease tests |
| candidate ownership | 原子提交点唯一转移；token/receipt 消除取消歧义；close 尽力且聚合错误 | cancel-before/after-commit、double-install、partial-close tests |

### 16.2 P1 评审矩阵

| 阻断项 | 规范决策 | 验收门禁 |
| --- | --- | --- |
| 两阶段 inference lease | infer-finally 独占 ACTIVE lease release；外部 release 仅取消/等待；reaper 只处理 READY | success/error/cancel/unresponsive-provider、fake-clock、concurrent-release tests |
| drain 资源上限 | timeout 只告警；归零继续关闭；最多两代 retired，满额拒绝 reload | retired-capacity、late-drain、diagnostics、shutdown tests |
| 乱序 reload | latest-request-wins；提交同时校验 CAS、latest sequence 与当前 source revision | slow-N/fast-N+1、slow-valid-N/fast-invalid-N+1、source-changed tests |
| activation result 清理 | pending 不淘汰；所有 final 保留 24h；4096 窗口容量；认证 token 的 expired disposition不依赖 acknowledge | capacity、TTL、missing-ack、expired/unknown、shutdown-pending tests |
| 应用配置原子性 | Product `AtomicApplicationComposition` 原子发布 shared model handle、窄 Runtime view 与 Product resources | pure-UI model reuse、MCP/skills readiness、same-turn lease、fault injection tests |
| 可运行迁移 | 每阶段 import/startup/test 全绿；同一请求不得跨新旧闭环 | 每阶段 startup/subsystem/integration/architecture green gate |
| 暗装切换 | C 新容器仅测试；D consumer 注入化且生产仍由旧 owner 经唯一 migration port 服务；E 原子切 root/owner/gateway 并删除 port | production-root reachability scan + C/D legacy-only production tests + E final-state tests |
| 初始启动 | `EMPTY -> ACTIVE -> SHUTTING_DOWN -> CLOSED`；首装使用 `ExpectedEmpty`，成功后才开放 admission | first-failure/cancel/double-first/empty-shutdown/startup-admission tests |

### 16.3 P2 最终开工矩阵

| 阻断项 | 规范决策 | 验收门禁 |
| --- | --- | --- |
| layered mode merge | mode 变化 replace subtree，同 mode overlay，provenance/secret 同步清除 | cross-mode、all input channels、untrusted canary tests |
| canonical codec | `mote.model-topology/v1` 固定 JSON subset，禁止 float，duration ms/decimal string | cross-version golden bytes/hash |
| 输入迁移 | 1.2 shortcut-only warning；1.3 missing-mode hard failure；离线迁移器不展开 secret | version/template/migrator fixtures |
| RouteId 历史 | v2 writer only；v1 按记录上下文 upcast，歧义的 resume fail closed | journal/rollout/routing/checkpoint replay fixtures |
| 运维契约 | typed operational events、低基数 metrics、明确健康状态与统一 redaction | event/metric/health/redaction capture tests |
| 阶段 0 | 固定 commit + 当前路径逐 hunk人工反向修改，禁止整文件 reset | manifest diff audit + 第 24 节命令 |
| 分阶段测试 | A–F 固定目录、smoke、架构、容量/并发/secret 门禁 | 第 25 节命令与禁止新增 skip/xfail |
| model handle reuse | 四元 reuse key 全等才 retain；先比较后创建 handles，失败/淘汰全关闭 | epoch/catalog/factory变化、no-create-on-reuse、loser-close tests |
| hostname canonicalization | v1 只接受 ASCII host/已成型 A-label，拒绝 Unicode，不使用 IDNA 实现 | Unicode reject、A-label/IPv4/IPv6 golden vectors + import guard |

### 阶段 0：恢复基线

严格按第 24 节 manifest 逐 hunk 恢复实验前行为，禁止文件级 checkout/reset。实验 normalizer
不得作为 adapter 留下。当前模型域最小回归已确认该实验造成
`42 passed, 1 failed`，失败表现为 default endpoint 丢失 tools capability；必须先恢复，
不得在后续实现中把该失败改写成新预期。完成条件是第 24 节固定命令通过，不使用“全量测试”
作为不可执行替代。

### 阶段 A：输入与 canonical contracts

1. 在 `product/config/model/` 新增 `ShortcutModelsConfig | ExplicitModelsConfig`；旧
   contracts `ModelsConfig` 暂时保留且旧生产路径行为不变。
2. 在 `contracts/model/` 建立 ModelTopology、RouteId、GenerationId、
   DefaultModelMetadata。
3. 建立版本化 canonical codec 与 validator。
4. 实现第 19 节 mode-aware merge/provenance 与第 21 节 1.2 compatibility reader；通用
   `deep_merge()` 不得特殊猜测模型 schema。
5. 阶段结束必须保持所有 import 与现有生产路径可运行，并通过新增 contracts/Product 输入
   测试和原有模型域回归。

### 阶段 B：原子 compiler 产物

1. 定义 Product-owned `SecretHandle`、`CredentialLease`、`CredentialBindingSpec` 和
   `CompiledModelGeneration`。
2. 实现唯一公开 `build_or_reuse_model_generation()` 及其私有
   `compile_model_generation(public_plan, secret_handles)` materialization helper。
3. 完成 capability/transport/context/slot 解析和一致性检查。
4. 覆盖 compression、OAuth、未知模型和 mixed-mode 测试。
5. compiler 与旧路径暂时并存但不接入生产；新路径必须在隔离集成测试中形成从 Product
   输入到完整 Runtime candidate 的闭环，Runtime 不得混合解释两套产物。

### 阶段 C：Runtime generation lifecycle

1. 新增 Product-layer `AtomicApplicationComposition` lifecycle、InferenceTargetLease、
   activation ledger、latest-request CAS 与 retired budget，但只接入隔离测试 composition root。
2. 实现 candidate build、atomic swap、drain、shutdown 和失败收敛。
3. snapshot 改为只收 ModelTopology，不做语义推断。
4. Product 使用 CredentialBindingSpec 构造 resolver 端口并转移资源所有权；Runtime
   只收 resolver 端口，不收 CredentialBindingSpec。
5. 阶段 C 不改任何 production composition root、gateway 或 consumer；旧
   `AtomicModelRuntime` 仍是生产唯一 owner。新旧容器可以同时存在于代码库，但新容器只能被
   isolation tests 引用，架构测试禁止它进入 Engine/CLI/watcher/managed production roots。
6. 阶段结束旧生产闭环与新隔离闭环各自全绿，任何请求都不能跨两个闭环。

### 阶段 D：迁移整个 Runtime

1. planner/gateway/cognition 改为只依赖可注入的 contracts composition/inference port，不在
   consumer 内 acquire 全局 owner。
2. Role、ContextProvider、context component、client context、提示与诊断改用同一注入端口。
3. 逐个删除 `runtime/**` 对 `ModelsConfig/default/tasks` 的读取；每迁移一组 consumer 即运行
   该子系统及直接依赖方测试。
4. 子 Agent 改为 RouteId selection。
5. Runtime 与 integration fixtures 改用 canonical builder。
6. production root 暂不启用新容器。为保持生产可运行，只允许在旧 production root 定义一个
   明确标记、架构 allowlist 管控的 migration port implementation，把新 consumer port 调用完整
   委托给旧 owner；它不得调用新 compiler、生成新 topology 或混合两套 route 语义。
7. migration port 仅存在于 Product composition root，Runtime consumer 不得知道其 legacy
   身份；其唯一删除门禁是阶段 E 原子切换，不能保留到 F。

### 阶段 E：统一装配与 reload

1. Engine、CLI、watcher、managed reload 共用 composition builder。
2. 在一个不可分割提交中：切换全部 production roots、启用
   `AtomicApplicationComposition`、删除/停用 `AtomicModelRuntime` current pointer、删除旧
   gateway 内部 acquire，并删除阶段 D migration port。禁止分批发布这些动作。
3. shared model handle、窄 Runtime Role view 与 Product resources 此时才由 application
   pointer 发布，不保留任何 root 使用旧生产语义。
4. 增加 initial activation、target lifecycle、乱序 reload、CAS、ledger、retired budget、
   shutdown 与原子 Role
   config 发布测试。
5. 阶段结束全量生产入口只走新闭环，旧类型仅剩无调用引用，所有测试通过。

### 阶段 F：删除迁移残留

1. 静态和运行测试确认生产代码对旧 `ModelsConfig`、实验 normalizer、旧迁移入口零引用后，
   在同一阶段一次性删除它们及旧 tests；不得在阶段 A 提前删除。
2. 删除 `_legacy_descriptor` 与运行时双解释 branches。missing-mode compatibility reader 只有
   当发布目标已到 `1.3.0` 才删除；若实现随 `1.2.x` 发布，则按第 21 节保留在 Product 边界，
   不得为了阶段 F 形式清零而提前删除。
3. 删除 Product resolver 对 source config 的读取。
4. 删除所有 model-name direct client creation。
5. 清零模型域 Runtime 生产代码中的 legacy/backward/compat；版本化 Product 输入 reader 与
   历史 RouteId upcaster 按第 21/22 节门禁管理，不计为双运行语义。以第 25 节阶段 F 固定门禁
   通过作为完成条件。

迁移共存只允许发生在阶段边界清晰、测试可识别的装配入口：C 的新路径从 Product compiler
到 Runtime composition 完整闭环但不可达生产；D 的生产请求仍完整使用旧 owner，唯一
migration port 只适配调用形状而不解释配置；E 在一个提交中切换并删除该 port。禁止 Runtime
在一次请求、一次 generation 或同一个 config object 内同时运行新旧解释语义。每个阶段结束
都必须可 import、可启动且相关测试全绿；任何红灯阶段不得继续叠加下一阶段改动。

## 17. 硬性验收标准

- `runtime/**` 禁止 import `ProductModelsConfig`、`ShortcutModelsConfig`、
  `ExplicitModelsConfig`、`ModelsConfig`、`CompiledModelGeneration`、
  `CredentialBindingSpec`、`SecretHandle` 或 `CredentialLease`。
- Runtime 不调用 `resolve_api_type`、capability/model profile catalog 或 credential
  source resolver。
- `build_or_reuse_model_generation()` 是 Product composition root 唯一入口；只有其私有
  materialization 阶段可调用 `compile_model_generation()` 产生 topology/bindings。
- model handle 只在 topology revision、credential epoch、provider catalog revision、adapter
  factory revision 全等时复用；reuse 前禁止创建 SecretHandle。
- topology 与 credential bindings 的 endpoint/slot 集合完全一致。
- `AtomicApplicationComposition` 是唯一 current pointer；现有 `AtomicModelRuntime` 和 gateway
  不得保留第二 owner。Runtime model generation 只作为 ref-counted resource 存在。
- 最终容器状态机严格为 EMPTY/ACTIVE/SHUTTING_DOWN/CLOSED；首装使用 `ExpectedEmpty`，初代
  activation 成功前禁止开放任何服务 admission 或健康状态。
- Product 从 topology/bindings 构造 Runtime candidate，再与 RuntimeRoleConfigView 和 Product
  resources 组成 application candidate 并原子安装。
- candidate 任意失败均保持上一 generation 完整可用。
- inference 只能通过 generation-bound lease gateway；一次调用从 route selection 到 journal
  attribution 只 acquire 一次，并携带 generation fencing identity。
- 两阶段 inference 使用单次消费的 `InferenceTargetLease`；READY 可由 release/reaper 释放，
  ACTIVE 只能由 infer-finally 释放。ACTIVE 外部 release 只能取消并等待 completion。
- CredentialMaterial 只承诺框架内短生命周期、固定脱敏和唯一 adapter wire-read seam；不
  宣称 Python/SDK 副本可消除或安全擦除。
- candidate ownership 只在 pointer swap 转移；activate 以 token/receipt 消除取消歧义，
  candidate 只可消费一次，关闭全部资源并聚合错误。
- reload commit 同时校验 expected application generation CAS、
  `reload_sequence == latest_requested_sequence` 与当前 source revision；最新请求失败时旧请求
  仍不得提交。
- activation ledger 不淘汰 pending；所有 final result 至少保留 24 小时且不依赖 acknowledge，
  expired 有效 token 明确返回 caller-must-not-close ownership disposition。
- 最多保留两个未关闭 retired application generations；超限拒绝 reload，绝不强关 inflight
  generation。
- shared model handle、`RuntimeRoleConfigView` 与 Product resources 由 application pointer 原子
  发布；纯 UI 变化复用 model handle。
- shortcut/explicit 模式只由输入 union 决定，不受默认字段影响。
- 子 Agent 只能引用 active generation 中已有的类型化 RouteId。
- revision 使用版本化 canonical serialization。
- readiness v1 仅做本地可构造性检查，禁止 provider/OAuth 网络调用。
- Runtime 中不存在 `models.default`、`models.tasks`、`if models.endpoints` 或
  legacy model adapter。
- 全部 compiler、Runtime、reload、Role、integration 和 architecture tests 通过。

## 18. 架构守卫

新增静态测试：

- 扫描 `runtime/` 禁止 Product 模型输入 import。
- 扫描 `runtime/` 禁止 `resolve_api_type` 与 capability catalog import。
- 扫描 Role/ContextProvider 禁止 `.config.models`。
- 限制 `compile_model_generation` 只能定义一次且仅被共享 builder 调用；所有 Product
  composition root 必须调用唯一 `build_or_reuse_model_generation()`。
- 禁止以 topology revision 单独决定 model handle reuse；reuse decision 必须构造并比较完整
  `ModelGenerationReuseKey`，且 reuse 分支不得引用 SecretHandle factory。
- 禁止裸字符串 route 穿过 Runtime 公共边界。
- 禁止 CredentialBindingSpec 出现在 serialization、event、session、logging schema 中。
- 禁止 Runtime import Product secret/binding 类型。
- 最终态禁止 Runtime、gateway 或 Engine 维护 `AtomicApplicationComposition` 之外的 current
  generation pointer；Runtime model handle 不得提供 current/swap API。
- 禁止 generation-bound gateway 或 `ModelRoute` 持有 composition acquire port；扫描 gateway
  execute 路径不得再次 acquire。
- CredentialMaterial 的底层值只能在 Product adapter 的 `read_for_wire` seam 读取；禁止其
  类型进入 serialization、journal、event、exception 与 telemetry schema。
- candidate 的 activation 状态、token receipt、取消结果和 close 聚合错误必须有并发/故障
  注入测试。
- 禁止两阶段 inference target 直接持有全局 gateway；target registry 的 TTL、容量和 release
  状态机必须使用可控时钟测试。
- target 状态机测试必须证明外部 release/reaper 无法释放 ACTIVE lease，且不可取消 provider
  会进入 shutdown timeout report。
- reload 入口必须经过唯一 coordinator 分配 `ReloadSequence`；禁止 composition root 直接
  绕过 CAS 调用低层 pointer swap。
- commit guard 必须读取 coordinator latest requested sequence 与 source 当前 revision；仅检查
  last committed sequence 不合格。
- Role 禁止保存可独立替换的 config current pointer；每 turn 的运行可见配置来自固定
  application lease 的 `RuntimeRoleConfigView`。
- 阶段 C 的 reachability test 必须证明新容器只被 isolation test roots 引用，所有 production
  roots 仍只到达旧 owner。
- 阶段 D 只 allowlist Product composition root 中唯一 migration port；禁止新容器进入生产、
  禁止 Runtime consumer import legacy owner，并证明 production 请求只触达旧 owner。
- 阶段 E 在同一变更中删除 migration allowlist、旧 current owner 和 gateway internal-acquire；
  此后启用最终态“唯一 application pointer”守卫。阶段 F 再删除无引用旧配置类型。
- 状态机测试必须覆盖 ExpectedEmpty/ExpectedActive、双首装、首次取消、EMPTY shutdown、
  SHUTTING timeout 和 CLOSED admission。
- canonical codec v1 禁止 import/call Python idna codec 或第三方 `idna`；非 ASCII hostname 必须
  有 rejection golden test，ASCII A-label 必须有 byte-stability golden test。
- 检查 invocation/routing 内部字段使用 RouteId；仅 codec/journal/event wire schema 允许
  稳定 route string。

这些守卫必须与迁移同步落地，不能等清理完成后补测。

## 19. Mode-aware layered merge

`models` 是带 discriminator 的语法子树，不能使用通用 `deep_merge()` 直接折叠。所有 file、
profile、env、CLI、programmatic 和 managed layer 先各自形成完整 raw layer，再由同一个
`merge_product_models()` 左折叠：

0. 仅在 1.2 compatibility reader 中，每个 layer 的 `models` subtree 在 merge 前执行 missing-mode
   检查：shortcut-only 字段得到 synthetic `mode: shortcut` 和原 source provenance；explicit-only
   或 mixed 字段立即失败。1.3 删除此步骤。
1. layer 不含 `models`：保持当前 models 与 provenance。
2. layer 含 `models` 但不含 `mode`：只有当前 mode 已确定时才按该 mode 做字段 overlay；overlay
   出现另一 mode 的专属字段立即失败。
3. layer 显式 `mode` 且与当前相同：在该 union variant 内字段级 overlay。
4. layer 显式 `mode` 且与当前不同，或当前尚不存在：整个 `models` subtree replace，绝不 deep
   merge；旧 variant 的 default/tasks/endpoints/pools/routes/recovery 与所有 credential 字段均
   不继承。
5. replace 时先删除 provenance 中 `models` 与 `models.*` 的全部旧 entry，再只记录新 layer
   实际提供的路径；缺失必填字段由 union validation 报错，不能从旧来源补齐。

安全顺序固定为 `parse layer -> strip_sensitive(untrusted layer) -> mode-aware merge -> resolve
trusted secret handles -> validate union`。WORKDIR 等 untrusted layer 即使触发 replace，也只能
用已经去除 `api_key/base_url/oauth/model_providers/api_key_helper` 的 subtree 替换；不能借 replace
继承、恢复或注入 credential。只设置 mode 的 env/CLI/programmatic layer同样触发 replace，若
该层没有提供新 variant 的完整必填字段则明确失败；各输入通道不得复制另一套 merge 规则。

现有通用 key denylist 不是新模型 schema 的充分安全边界。A 阶段必须增加 schema-aware
`strip_untrusted_model_credentials()`：移除整个 `credential_pools`、endpoint inline secret/OAuth
material、helper/ref/token/client_secret 及未来由 Product credential input union 标记为 sensitive
的字段；在 mode replace 前执行。该函数由输入 schema 的 sensitive metadata 驱动，新增
credential variant 未声明 metadata 时架构测试失败，不能靠维护另一份易漏字符串列表。

错误包含 layer source、配置文件路径（对外诊断使用安全相对/逻辑 source，不泄露宿主绝对
路径）、`models.mode` provenance 和冲突字段路径。测试至少覆盖 shortcut→explicit、
explicit→shortcut、同 mode overlay、空 map、CLI/env/programmatic mode replace、provenance
清除以及 trusted/untrusted secret 不残留。

## 20. Canonical topology codec v1

topology revision 的唯一输入是 `mote.model-topology/v1` canonical document，不使用 Pydantic
默认 dump 或通用 `json.dumps` 的版本相关行为：

- root 必须包含 `"schema":"mote.model-topology/v1"`；hash 仍为
  `sha256("mote-model-topology-v1\0" + canonical_bytes)`。
- 文本先校验为合法 Unicode scalar sequence，再做 NFC normalization，最后编码 UTF-8，无 BOM。
- object key 同样 NFC；normalization 后重名即失败。key 按其 UTF-8 bytes 无符号字典序排列。
- array 保持领域定义顺序；语义为 set/map 的集合必须在 canonical model 构造时转成 object，
  或按其 typed ID 的 canonical UTF-8 bytes 排序，不能依赖输入/insertion order。
- 每个 v1 schema 字段都必须出现。optional 值使用 JSON `null`；缺字段非法；不得因默认值而
  omit。bool 固定 `true/false`，integer 使用最短十进制、无 `+`、无前导零且禁止 `-0`，enum
  使用规范中固定的小写 ASCII wire string。
- v1 禁止 IEEE float。所有 duration/timeout/backoff 先校验范围并转为整数毫秒；不能精确到
  毫秒的输入失败。pricing/ratio 若需要小数，使用无 exponent、无前导 `+`、无无意义前导或
  尾随零的 canonical decimal string，零只能写 `"0"`。
- endpoint URL 使用 RFC 3986 absolute URI。v1 不执行 Unicode→IDNA 转换：hostname 必须是
  ASCII，Unicode hostname 直接以 `NonAsciiHostnameError` 拒绝；国际化域名必须由输入方预先
  提供小写 ASCII A-label（如 `xn--...`）。codec 将 DNS host 转小写并只校验 ASCII label
  长度/字符/连字符位置，把合法 A-label 当作稳定 opaque label，不调用 Python 内建 idna codec
  或任何传递依赖。IPv4 使用最短 dotted decimal，IPv6 使用 RFC 5952 并保留方括号；移除
  http:80/https:443，空 path 变 `/`，移除 dot segments，percent hex 大写并解码 unreserved
  bytes。userinfo、query、fragment 在 topology base URL 中禁止；provider query 参数必须进入
  单独的已类型化 map。Unicode→IDNA 若未来需要，只能新增 v2 codec/schema，不能修改 v1。
- topology 内 RouteId 使用 tagged object：`{"kind":"default"}`、
  `{"kind":"task","name":"..."}`、`{"kind":"semantic","name":"..."}`。`default`、
  `task:*`、`semantic:*` wire string 只用于 journal/event/session 边界，不参与 topology hash。
- JSON token 固定为 RFC 8259 紧凑形式：无多余空白，string 使用双引号，控制字符使用小写
  `\u00xx`，其余非 ASCII Unicode 直接输出 UTF-8；禁止 NaN/Infinity 和重复 key。

`v1` codec 实现必须在 `ztest/config/fixtures/model_topology_codec_v1.json` 保存手写 golden
vectors，覆盖 Unicode 等价串、key 顺序、list 顺序、null、
bool/int、毫秒、decimal、URL、三种 RouteId 和非法 float。vectors 保存 canonical bytes 与完整
SHA-256，分别在最低/最高支持 Python 与 Pydantic 版本运行；升级依赖不得改写 v1 bytes。未来
变化新增 `v2` codec/前缀，不能原地修改 v1。

URL golden vectors必须额外覆盖：ASCII host 大小写、预编码 A-label 原样稳定、Unicode host
拒绝、IPv4/IPv6、default port、dot segments 与 percent normalization。`pyproject.toml` 不新增
`idna` 直接依赖，v1 代码也禁止 import `idna` 或调用字符串 `.encode("idna")`。

## 21. Product 输入迁移版本门禁

版本计划以当前 `pyproject.toml` 的 `1.1.0` 为基线：

| 版本 | 无 mode 行为 | 生成器/迁移工具 |
| --- | --- | --- |
| 1.1.x | 现状，仅作为迁移输入基线 | 不生成新格式 |
| 1.2.0 | 仅 shortcut-only 字段可推断 shortcut，并发一次去重 warning；explicit-only/mixed 失败 | 模板永远写 mode；提供 `mote config migrate-models --check` 与 `--write` |
| 1.3.0 | 缺 mode 一律失败 | 工具仍可离线迁移，但 loader 不再推断 |

迁移命令使用与生产 loader 相同的 discovery、trust、mode-aware merge 和 provenance 逻辑；
`--check` 不写盘并输出将选择的 mode/文件，`--write` 只修改用户明确指定的文件、写 before-image
备份并拒绝 mixed/ambiguous 输入。它不得把合并后的 secret 展开回 YAML，也不得把高层 secret
写入低层文件。warning/error 至少包含逻辑 source、相对配置路径和 `models` provenance；secret
值与绝对 home path 必须脱敏。阶段 F 删除的是在线推断分支，不删除离线迁移器；删除门禁是
package version 已为 1.3.0、1.2 compatibility tests 已冻结为 fixtures、所有新模板均显式 mode。

## 22. RouteId 历史数据兼容

新 writer 从 schema v2 起只写 canonical wire string `default/task:*/semantic:*`，并在 journal、
rollout/event、routing decision、checkpoint 和 diagnostics export 各自 envelope 中写
`route_schema_version: 2`。内部仍立即 decode 为 typed RouteId。

旧 schema v1 是未加 namespace 的裸字符串。upcaster 只能使用记录类型中已有的确定性上下文：

- `default` 唯一 upcast 为 `DefaultRoute`。
- 已带 `task:`/`semantic:` 的值按 v2 严格 decoder 处理。
- v1 routing-decision 记录天然属于 semantic routing，可将非 default 裸名 upcast 为
  `SemanticRoute(name)`。
- v1 checkpoint/task invocation 只有存在持久化 task-kind/task-name 字段时才可 upcast 为
  `TaskRoute`；存在明确 semantic marker 时才可 upcast 为 `SemanticRoute`。
- 仅有一个非 default 裸名时无法区分同名 task/semantic，禁止查询当前配置猜测，也禁止默认
  选择 task。该记录标记 `AMBIGUOUS_LEGACY_ROUTE`。

失败策略按用途固定：resume/checkpoint/routing-state 等影响执行语义的记录遇到 ambiguous 或
invalid route 时终止 resume，返回 `UnsupportedHistoricalRouteError` 和安全诊断；已完成的
model-call journal、rollout observation、diagnostics export 可跳过该单条 route projection，
并在 operational EventBus 发 `HistoricalRouteUpcastRejected`，原始 durable bytes 不修改。
未知 schema version 同样 fail closed，不能按 v1 猜测。新 writer 永不降写 v1。

v1 reader 属于十年历史读取契约，不在阶段 F 删除。只有新的 major-version ADR、官方离线
migrator、所有受支持 storage 扫描为零 v1/ambiguous records、备份与回滚演练完成后才可删除。
测试必须提交真实形状的旧 model-call journal、rollout/event、routing decision、checkpoint
fixtures，覆盖 replay、resume、default、确定 task/semantic、同名歧义、坏值和未知版本。

## 23. 可观测性与健康契约

在 `contracts/events/` 定义 typed operational events，全部进入非持久化 operational EventBus；
模型调用事实仍由既有 durable journal/session plane 负责，lifecycle observer 不重复写业务
事实：

| Event | 必需的非敏感字段 |
| --- | --- |
| `ApplicationActivationRequested` | activation token fingerprint、reload sequence、source revision |
| `ApplicationActivationCommitted` | application/runtime generation ID、topology/source revision |
| `ApplicationActivationRejected` | sequence、reason code（validation/cancelled/closed） |
| `ApplicationActivationStale` | candidate/latest sequence、source revision mismatch flag |
| `ApplicationActivationCasConflict` | expected/current application generation ID |
| `ApplicationReadinessFailed` | component kind、redacted error code |
| `RetiredGenerationCapacityReached` | retired count/limit、oldest age bucket |
| `GenerationDrainTimedOut/Completed` | generation ID、lease count、age/duration |
| `InferenceTargetExpired/CapacityReached` | target state/count/limit、age bucket；不含 prompt |
| `CompositionCloseFailed` | resource kind、redacted resource identity、error code/count |
| `ApplicationShutdownTimedOut` | generation/lease counts、oldest age bucket |

metrics 是事件的低基数投影：activation/readiness/stale/CAS/target-expired/capacity/close/shutdown
使用 counter；active/retired generation、READY/ACTIVE target、lease count 使用 gauge；compile、
readiness、drain、activation duration 使用 histogram。metrics label 只允许固定枚举
`result/reason/component/state`；generation ID、route ID、endpoint ID、source revision/path、
credential identity、model 名和异常文本禁止作为 label。

健康状态由容器状态与资源告警确定：EMPTY=`not_ready`；初代安装后的 ACTIVE=`ready`；ACTIVE
出现 drain timeout、close failure、retired/target capacity reached=`degraded`；告警资源清零且
没有 close debt 后恢复 `ready`；SHUTTING_DOWN=`shutting_down`；CLOSED=`closed`。stale/CAS
conflict 和单次 readiness rejection 本身是预期控制结果，不降级当前健康 generation；若因此
仍处 EMPTY 则保持 not_ready。

事件 payload、diagnostic 和自动日志统一经过 redaction policy：credential/material/identity、
token、用户 home/绝对 source path、provider exception body 不得原样输出；异常转为稳定 error
code 与清洗后的 summary。关键 coordinator/container/registry/reaper 类使用
`@log_class(level="DEBUG", exclude={...热路径...})`；禁止新增 method-body `logger.*`。EventBus
失败不得改变 activation/close 状态机，但记录到既有 telemetry failure counter。

## 24. 阶段 0 精确恢复 manifest

当前工作区包含大规模目录迁移，禁止按文件整体 checkout/reset。恢复行为基准固定为 commit
`0bfb8b2a0fc7705af165641a328c32958b358d01` 中对应旧路径，加上当前路径重定位；恢复方法是
人工逐 hunk 反向修改，不运行 checkout/reset，也不反向应用整个文件 diff。执行前用
`mktemp -d` 建立专用 preimage 目录，复制下表所有当前文件（包括 untracked 文件），并保存
`git status --short`、目标 tracked 文件的 `git diff --binary`、每个 preimage 的 SHA-256；完成
后报告临时目录路径。不得覆盖同文件中的 import/package migration 或用户改动。

| 当前路径 | 只恢复的实验 hunk | 依据 |
| --- | --- | --- |
| `product/models/topology.py` | 删除实验 `compile_model_topology()` 文件 | 当前 untracked 实验文件，无 HEAD 定义 |
| `product/models/__init__.py` | 只移除 topology import/`__all__` export；保留其他目录迁移内容 | 与上项引用闭包一致 |
| `product/config/loader.py` | 移除 topology import 和 `_build_config()` 中 `config.models = compile_model_topology(...)`，恢复直接返回 typed Config | `HEAD:runtime/config/loader.py::_build_config` |
| `product/models/bootstrap.py` | `builtin_model_gateway()` 与 `reload_builtin_model_gateway()` 移除 `compile_model_topology()` 调用，直接把原始 `models` 交给 snapshot/resolver；保留当前文件拆分与其他 bootstrap 迁移 | `0bfb8b...:product/integrations/bootstrap.py` 对应两个 composition roots |
| `runtime/models/failover/snapshot.py` | 恢复 `if models.endpoints` declarative 分支及无 endpoints 的 `_legacy_descriptor`、singleton groups/routes/direct slots、legacy public shape；不回退当前 package import 重定位 | 该文件 HEAD 对应函数的行为 diff |
| `product/models/gateway.py` | `_bindings()` 恢复 endpoints/shortcut 双分支与 `direct_credential_slot_ids`；不回退文件重定位 | `HEAD:product/integrations/models/endpoint_resolver.py::_bindings` |
| `ztest/router/llm/test_failover_planner.py` | 移除 topology import；恢复 legacy test 名与两处直接 `build_model_runtime_snapshot(models)` | 该测试 HEAD hunk |
| 其他实验测试 | 删除只验证自动 normalizer 的新增测试；保留目录/import migration与既有 gateway tests | 逐 hunk review，不按文件回退 |

经源码比对，当前 `runtime/agent/components/cognition.py::_task_route_map` 与
`HEAD:runtime/agent/runtime_modules/cognition.py` 的 endpoints/shortcut 双分支行为一致，当前
没有可归因于本实验的反向 hunk；阶段 0 不修改它。`ztest/router/llm/test_runtime_model_gateway.py`
当前 diff 主要是 package relocation/formatting，未发现 `compile_model_topology` 接入；不得纳入
笼统回退。若执行前 diff 与本 manifest 不一致，阶段 0 立即停止并重新评审 manifest。

恢复验证命令固定为：

```bash
python -B -m pytest \
  ztest/config/test_loader.py \
  ztest/config/test_layers.py \
  ztest/config/test_model_failover_config.py \
  ztest/router/llm/test_failover_planner.py \
  ztest/router/llm/test_runtime_model_gateway.py \
  ztest/router/llm/test_product_endpoint_adapters.py \
  -q --tb=short -p no:cacheprovider

python -B -m pytest \
  ztest/architecture/test_layer_dependencies.py \
  ztest/architecture/test_model_gateway_boundary.py \
  ztest/architecture/test_local_imports.py \
  -q --tb=short -p no:cacheprovider
```

验收是实验前 shortcut/default tools 行为恢复且相关集合全绿；已知 `ztest/prompts` 收集问题不在
这些命令中。不得通过改预期、xfail/skip 或保留 hidden normalizer 使测试变绿。

## 25. 逐阶段固定测试门禁

所有命令使用 `python -B -m pytest ... -q --tb=short -p no:cacheprovider`。每阶段先运行该阶段
新增测试，再运行下表固定范围；失败立即停止，不叠加下一阶段。所有阶段额外执行
`python -B -c "import mote; import mote.product.entrypoints.cli.__main__"` import smoke。startup
smoke 使用测试内的临时配置、fake provider/secret source 和无网络 Engine/CLI harness，不启动
真实 provider、不消费 credential/费用。

必须新增并固定以下 smoke/guard 文件名：

- `ztest/integration/test_model_composition_startup.py`：EMPTY、初代安装、Engine admission、reload。
- `ztest/cli/test_model_composition_startup.py`：CLI 初代失败退出码、成功后才 ready。
- `ztest/architecture/test_model_composition_reachability.py`：C/D/E production-root reachability。
- `ztest/session/fixtures/model_routes_v1/`：journal/rollout/routing/checkpoint 历史 fixtures。
- `ztest/config/test_model_topology_codec.py`：读取 golden vectors 并校验 canonical bytes/hash。

| 阶段 | 固定 pytest 范围 | 额外硬门禁 |
| --- | --- | --- |
| 0 | 第 24 节两条命令 | shortcut tools/OAuth 基线；工作区 diff manifest audit |
| A | `ztest/config ztest/architecture/test_contracts_governance.py ztest/architecture/test_layer_dependencies.py ztest/architecture/test_product_dependencies.py` | mode merge/provenance/untrusted secret；codec golden vectors含 Unicode host拒绝/A-label稳定；import smoke |
| B | `ztest/config ztest/router/llm/test_failover_planner.py ztest/router/llm/test_product_endpoint_adapters.py ztest/architecture` | compiler determinism、reuse key四维变化、no-handle-on-reuse、失败/并发 loser handle close、redaction capture；隔离 builder startup smoke |
| C | `ztest/router/llm ztest/events ztest/runtime/test_lifecycle.py ztest/architecture` | 新容器仅 test-root reachability；fake-clock target/ledger/retired limits；fault injection；startup EMPTY tests |
| D | `ztest/router ztest/roles ztest/session ztest/runtime ztest/architecture` | legacy-only production reachability；唯一 migration-port allowlist；journal/rollout/checkpoint v1 fixtures |
| E | `ztest/{roles,flow,executor,think,context,skills,router,agents,automation,background_tasks,workflows} ztest/config ztest/session ztest/events ztest/runtime ztest/integration ztest/cli ztest/architecture` | Engine/CLI initial-install and reload smoke；old owner不可达；concurrency/shutdown/resource-limit/performance tests |
| F | 与 E 相同 | `rg` 零 Runtime 旧类型/normalizer/migration-port/双解释 branch；发布目标为 1.3+ 时验证 missing-mode rejection，1.2.x 时验证 warning/expiry gate；离线迁移 fixture |

pytest brace form若 shell/平台不展开，CI 必须展开为表中逐目录参数，测试集合不得缩小。阶段 E/F
的性能门禁使用 fake provider：target registry 1024、activation ledger 4096、retired limit 2 在
边界值和 +1 均验证；在四元 reuse key 固定时，100 次 UI reload 不得创建新
resolver/credential handle；
并发 resolve/infer/reload 测试不得泄漏 task/lease。时间性能只做基线回归阈值并固定 CI runner，
不能用宽松 wall-clock 替代容量不变量。

已知失败 allowlist 仅包含仓库约定的 `ztest/prompts/*` import 路径问题，并已由 pytest config
ignore；本机偶发 pytest AST recursion 只能用 `--tb=short/--tb=no` 重跑，不能记为产品 allowlist。
任何阶段禁止新增或放宽 `xfail/skip/filterwarnings/ignore`，除非单独评审并更新本规格。secret
redaction capture test 必须同时捕获 EventBus、`@log_class` 输出、异常、diagnostics、journal 和
metrics exporter，注入唯一 canary secret 后断言原值、前后缀、credential identity 与绝对配置
路径均不存在。
