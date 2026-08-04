# Mote 泛型与动态类型真实架构债务治理清单

状态：`CLOSED / VERIFIED`  
审计基线：2026-08-04 当前工作树  
生产范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/`

本文只记录已经沿当前生产源码、真实消费者以及 composition、wire 或 durable 数据流确认的架构债务。关键词命中、扫描数量、测试源码以及已经核实为有意设计的用法不进入本清单。

本次明确排除：

- D24：当前生产 `assert` 命中均位于显式 fail-closed 检查之后，或表达 owner 内部不可达状态；
- D30：当前 lambda/partial 命中均为局部函数、排序 key、default factory，或受已有完整 callback 类型约束；
- D33：当前内建 `id()` 命中只用于进程内对象去重、生命周期标签或诊断，没有进入 durable/resume identity。

以上排除只适用于已经核实的具体 symbol，不是未来同类用法的通配许可。

## 1. 已确认债务

### 1.1 模型、Gateway 与公开 Contract

| ID | 真实债务与证据 | 治理出口 |
|---|---|---|
| D0 | `runtime/models/failover/orchestrator.py` 将 provider 在 executor、selector、state、key、admission 和 observer 之间退化为 `Any/object`；`runtime/models/model_gateway.py` 是真实消费者 | 引入贯穿 `ProviderT + RequestT + ResultT` 的 typed attempt state、selector、transformer、observer 与 result 链；provider key 使用稳定 typed identity |
| D1 | `contracts/ports/model/gateway.py` 已闭合 keyword 参数，但 `GenerationBoundRuntimeModelGateway`、`ExactCachedModelGateway`、`CurrentRuntimeModelGateway` 的实现重新使用无注解 `**kwargs` | 所有实现与 Port 使用完全一致的显式参数；删除宽 kwargs，不增加兼容 wrapper |
| D2 | `contracts/hook/invocation.py` 的公开 Hook invocation 使用裸 `dict`，被 Runtime Hook、Product composition 和 wire 投影共同消费 | Contract 拆成版本化 invocation DTO 与 tagged outcome；arguments 使用 canonical JSON/typed command，permission 修改只能单调收窄 |
| D3 | `contracts/events/` 的 telemetry/session/model 公开事件包含 `Any`、裸 dict 或开放 mapping，并进入 Runtime publisher、codec 和 Product presentation | 按 domain 建立 authoritative event DTO；event encoder 显式列字段，开放 JSON 使用 canonical `JsonValue`，durable decoder 严格 fail closed |
| D4 | `contracts/conversation/messages.py` 的 canonical message 同时承担 provider projection 和 persistence `to_dict/from_dict` | 保留唯一 domain Message；provider wire 归 Product adapter，durable envelope/codec 归持久化 owner，删除混合生命周期入口 |
| D5 | `contracts/model/profile.py::json_schema_transformer` 是 `Callable[[dict], dict]` 开放扩展点，但全生产无消费者 | 删除字段、注释承诺和生产导出；不得为保留死扩展面补造消费者 |

### 1.2 Kernel、Workflow、BackgroundTask 与 Tool 执行链

| ID | 真实债务与证据 | 治理出口 |
|---|---|---|
| D6 | `kernel/inference/tokenization.py`、`kernel/commands/` 继续解释动态 message/command mapping；真实消费者包括 prompt/model adapter 与 native/XML command 链 | Kernel 只接收 canonical message/command tagged union；provider mapping 留在 Product adapter；partial/recovery 返回 typed result |
| D7 | `orchestration/workflows/` 混合 process-local callable graph 与 durable Workflow definition；宽 callable/type/reducer/state 被 Product RunGraph 和 recovery 消费 | 分离两种生命周期；durable definition 只保存版本化 definition identity/schema，不保存 callable/code object；process-local graph 用 ParamSpec/TypeVar/Protocol 保留 node/reducer 关系 |
| D8 | `orchestration/background_tasks/decorators.py` 通过私有 marker 和 `getattr` 发现 background capability，且 decorator 未保留 callable shape | 使用 definition-owned typed disposition；Product catalog 显式发布；decorator 用 ParamSpec，但不成为第二 registry 或执行入口 |
| D9 | `runtime/tools/tool_pipeline.py::ToolExecution.tool`、callable 和 arguments 退化为 `Any`，执行期 probing 位于 ToolExecutor permission/effect/audit 主链 | ToolExecutor 只接收 immutable typed binding snapshot 与 canonical invocation；signature inspection 只允许 Product 构建期；删除 live tool/cast/reflection 旁路 |
| D10 | `runtime/fileops/mutation/artifact_roots.py` 已注入 `ExternalArtifactRootSource` Protocol，却再次用 `callable(getattr())` 验证内部 capability | 内部来源由 typed composition 保证；仅外部 plugin adapter 可在入口验证，随后投影为 Runtime Port |

### 1.3 静态类型、mapping、codec 与 schema

| ID | 真实债务与证据 | 治理出口 |
|---|---|---|
| D11 | `static_governance.py` 与 `test_dynamic_boundary_governance.py` 重复事实；动态边界登记无人读取，当前门禁不能证明债务闭合 | 统一 scanner owner；建立精确 symbol baseline 和合法边界 registry 门禁；检查 new/stale/expired identity，不以测试内复制规则维持双真相 |
| D12 | `contracts/conversation/queue.py::MessageQueue.push/empty` 等 public surface 缺参数或返回注解，真实消费者得到隐式 `Any` | 为所有 governed public surface 和关键 private 链补全显式类型；framework hook 只有经登记才可保留动态输入 |
| D13 | `runtime/tools/capability_types.py` 中 `HandoffRuntime`、`WaitInterruptible`、`RunSkillFork`、graph output 和 image capability 使用 `Callable[..., R]` | 为每个 capability 定义精确 Protocol/Callable；用 ParamSpec/TypeVar 保留参数和 await result，不以宽 alias 汇总不同关系 |
| D14 | `contracts/events/_base.py`、`runtime/session/events.py`、inference API artifact projection 使用 `vars/asdict/model_dump` 自动生成 event、durable 或 public wire payload | 每个边界使用显式版本化 encoder；新增内部字段不得自动外泄；增加 extra/missing/unknown 字段负例 |
| D15 | Workflow type/reducer 与 tool definition registry 使用裸 `type`、裸 `Callable`、`Any` 或缺泛型实参的异构 collection | 将异构项封装为 owner-owned typed binding；保留 type argument、variance、Coroutine/Awaitable result，禁止由 cast 恢复关系 |
| D16 | `ToolCallAction.arguments`、tool policy 和 ToolExecutor 使用 `dict[str, Any]`，但字段实际受 definition、permission target 和 effect digest 解释 | 未验证 JSON 只存在于 adapter；admission 后转换为 canonical typed invocation/immutable JSON，不让宽 mapping 穿过 permission/effect 链 |
| D17 | `RecoveryEvent.phase`、`SpanEndEvent.status`、`RuntimeHandoffResolution.status` 等已知闭集仍为普通字符串 | 由 domain owner 定义 enum/tagged union；wire 字符串只在严格 codec 投影，unknown value fail closed |
| D18 | `runtime/events/telemetry.py::_TypedTelemetryBinding.erase` 通过 cast 擦除 handler/event 泛型；登记所称 TypeGuard 与源码不符 | 异构 erasure 留在唯一 owner 的封闭 binding 中，并以不可伪造 token/dispatch 操作保证关系；不得声称 runtime Protocol/TypeGuard 能证明泛型实参 |
| D19 | `runtime/session/events.py` 多个 durable `from_payload` 接收 `Dict[str, Any]`，通过 `str/int` 恢复字段；JSON 动态 shape 离开 adapter | parse 结果在同一 codec 内严格检查 exact shape、primitive、version、tag，再构造 canonical event；动态值不得传给各事件自行猜测 |
| D20 | `runtime/resilience/breaker.py::CircuitBreaker.__init__.clock` 声明非 Optional Callable，却以 `None` 为默认并用 ignore 消音 | 修正真实签名或使用明确 sentinel/overload；删除 ignore，确保 implementation、override 和 decorator wrapper 与公开契约一致 |
| D21 | `pyrightconfig.json` 有 5 个失效旧路径 ignore，且整个 `product/inference/daemon/rpc` 被忽略 | 删除 stale ignore；生成文件使用精确 manifest，手写 wrapper 全部参与 Pyright；按 governed symbol 渐进收紧而非目录级豁免 |
| D22 | `product/workflows/agent_service.py::resume_plan` 仅校验 override key 后调用 `model_copy(update=...)`，绕过 durable state 字段验证 | 使用 strict typed transition/constructor；验证 primitive、variant 和跨字段不变量后再提交恢复状态 |
| D23 | `contracts/foundation/errors/base.py` 使用 pickle、`__new__ + __dict__.update`；conversation contract 使用 `SerializeAsAny` 并进入 session serialization | Contract/wire/durable 只承载版本化 ErrorReport、Message DTO、canonical JSON 或 artifact reference；Python object serializer 仅能位于不持久化的 adapter |
| D25 | SQLite metadata 与 session event decoder 对未验证字段使用 `str/int`，将错误 primitive 强转为合法值 | decoder 拒绝错误 primitive、required 缺失和未知字段；encoder 格式化与 decoder coercion 分开治理 |

### 1.4 状态、identity、immutability 与 capability

| ID | 真实债务与证据 | 治理出口 |
|---|---|---|
| D26 | `orchestration/background_tasks/model.py::BgTaskResult` 的 mode/result/poll_factory 可形成互相冲突或缺失组合 | 使用 foreground/background/hybrid tagged union；每个 variant 只携带合法字段，删除可任意组合的公共构造器 |
| D27 | task Port、`TaskSnapshot`、`_TaskState` 在已有 `TaskId/AttemptId` 的情况下仍用裸字符串传递 task identity | 从 Port、pool、handle、query、cancel、result 到 notification 保持 canonical identity；跨 Pool reference 同时绑定 Agent/process/incarnation/attempt |
| D28 | `MaterializedToolDefinition.input_schema` 位于 frozen DTO，却原样保存调用方 mutable dict | admission 时 deep-freeze/snapshot；query 返回 immutable projection，不通过 `Mapping` 注解伪装深层不可变 |
| D29 | background decorator marker 通过 setattr/getattr 成为跨 owner capability discovery | 与 D8 同一切片迁移到 immutable typed definition/catalog；删除 marker 和反射发现路径 |
| D31 | RunGraph 明确将 channel type 视为 advisory；`GraphState` 使用 `extra="allow"` 且不验证 assignment，动态 expression/node result 进入 Workflow state | compile definition 时形成严格 channel/input/output schema；每次 commit 前校验动态结果；错误结果进入 typed node failure，不写 state |
| D32 | protobuf typing 缺口导致整个 RPC 目录被 Pyright 忽略，同时遮蔽手写 client/server glue | 与 D21 同一切片：精确隔离 generator-owned 文件，以 typed wrapper 验证 generated object 与 canonical DTO 的转换 |
| D34 | Workflow definition 和 tool compiler 使用 module/class qualname 参与 durable definition digest 或 semantic identity | 使用 owner 明确声明、版本化且不随 rename 变化的 definition identity；qualname 仅作诊断信息 |
| D35 | RunGraph router/node 获得完整可变 GraphState；部分跨组件 callback 闭包完整 Role，却只消费少量字段 | 跨包/extension 使用最小 immutable input DTO、query/command Protocol；同 bounded context 内部协作不制造无语义 facade |
| D36 | `BaseConsumer.handle_sync` 先调用 handler，再判断返回值是否 coroutine 并关闭；多个 Hook/Tool lifecycle callback 运行时猜测 sync/async | 正式 callback 分成明确 sync/async Port；外部框架兼容只在 adapter 入口归一为 async binding；调用前确定 lifecycle，统一异常、取消和 cleanup |
| D37 | inference API 与 webhook API 的 11 个 aiohttp `AppKey` 把已知 Gateway/owner/verifier/sink 类型写成 `object`，读取时 cast | `AppKey[T]` 直接绑定 authoritative type；put/get 使用同一 T，错配在 composition/activation 前静态或 fail-closed 拒绝 |
| D38 | `ComponentKey[T]` 只保存公开 name，graph 按 name 查 slot 后 cast；同名错误 T 可以命中 | token 由 owner 创建并绑定不可伪造 nominal identity；注册与 retrieval 使用同一 token，字符串只作诊断/manifest identity，不能授权返回类型 |

## 2. 治理顺序

按依赖与风险执行，不能把全部债务做成一次无边界重构：

```text
G0  治理门禁：D11、D21、D32
 -> G1 模型泛型与 Gateway：D0、D1、D20
 -> G2 Contracts/Event/Conversation：D2-D5、D12、D14、D17、D19、D23、D25
 -> G3 Kernel command/message：D6
 -> G4 Workflow definition/state：D7、D15、D22、D31、D34、D35
 -> G5 Tool/BackgroundTask：D8、D9、D13、D16、D26-D29、D36
 -> G6 Composition typed key/token：D18、D37、D38
 -> G7 FileOps reflection：D10
 -> G8 全生产 baseline 清账与独立验收
```

D8/D29、D21/D32 是同一根因的不同验收面，应在一个切片内关闭，不能重复实现两套机制。

## 3. 每个切片的零债务要求

每个实施切片必须同时闭合：

1. canonical owner、authoritative type 与最小 Port；
2. 所有生产 consumers、composition 和 public exports；
3. lifecycle、permission、durability、wire 与 migration 决定；
4. 旧 DTO、mapping API、marker、cast、ignore、reflection 和 re-export 的删除；
5. Pyright pass/mismatch fixture、malformed runtime negative test、owner/composition gate；
6. 仓内消费者搜索和无平行入口证明。

禁止通过 alias、compat wrapper、双 decoder、fallback、目录 ignore、新 facade 或扩大合法动态边界登记来清零 baseline。

## 4. 最终验收

全部满足后才能声明本清单关闭：

- 上述真实债务逐 symbol baseline 为空；
- definition → builder/request → runtime → handle/result 的泛型关系不中断；
- Contract、Port、event、durable、permission 和 effect 边界没有已知 shape 的裸 dict、无界 Any 或裸 Callable；
- decoder 对未知 version/tag、额外/缺失字段和错误 primitive fail closed；
- state/result/lifecycle 的非法组合不可构造；
- typed key/token 的值关系不可由字符串、cast 或调用方伪造；
- ToolExecutor、Workflow engine、BackgroundTaskPool、event codec 和 composition 均只有一个 canonical 执行/状态入口；
- 合法动态边界只登记精确 symbol，并具有 owner、validation test 和未过期 review date；
- 相关架构门禁、type-contract verifier、Pyright、domain tests 与 Product lifecycle smoke 全部通过；未运行项明确记录为 `NOT_RUN`。

本轮治理未删除 durable 数据、未引入第三方依赖，也未保留兼容入口、双读写或旧 symbol alias。

## 5. 关闭台账（独立复验完成）

独立复验结论见 `zdocs/generic-dynamic-type-acceptance-rejection.md`。此前退回的项目均已完成
修改和聚焦复验；最后剩余的 **D3、D11** 已闭合，当前账台全部关闭。

`typecheck/dynamic-type-debt.json` 是本清单的 authoritative exact-symbol baseline；其
`governed_ids` 精确覆盖 D0-D23、D25-D29、D31-D32、D34-D38；schema v2 的
`records` 为每个 ID 保存 canonical owner、精确生产 symbol、真实 consumer 与 validation test。
门禁会拒绝缺项、ID 重复/乱序、陈旧路径及陈旧 symbol，不再把空 records 当作关闭证据。
`ztest/architecture/static_governance.py::check_confirmed_dynamic_debt_symbols` 校验该
envelope、ID 集合、生产 source roots 与下列 retired symbol；
`check_dynamic_boundary_registry` 另外校验合法动态边界的 exact file + qualified symbol、
owner、validation test、未过期 review date，以及所有运行时 awaitable probing 都已登记。

| ID | 状态 | canonical owner / 已关闭出口 |
|---|---|---|
| D0 | VERIFIED | `runtime.models.failover`：provider/request/result 泛型 attempt 链与稳定 provider identity |
| D1 | VERIFIED | `contracts.ports.model.gateway`：Port 与三种 Runtime Gateway 使用同一显式 keyword 签名 |
| D2 | VERIFIED | `contracts.hook`：版本化 invocation/outcome 与 canonical JSON arguments |
| D3 | VERIFIED | 四个登记 decoder 均覆盖 missing/extra/wrong-primitive 拒绝语义 |
| D4 | VERIFIED | `contracts.conversation`：Message、provider projection 与 durable codec 生命周期分离 |
| D5 | VERIFIED | `contracts.model.profile`：无消费者 schema transformer 已删除 |
| D6 | VERIFIED | `kernel.commands` / `kernel.inference`：只消费 canonical message/command union |
| D7 | VERIFIED | Workflow definition/state/deferred 主链参数化，durable executable contract 明确 |
| D8 | VERIFIED | `orchestration.background_tasks`：typed definition disposition，marker discovery 已删除 |
| D9 | VERIFIED | `runtime.tools`：immutable `ExecutableToolBinding` 与 ToolExecutor 唯一执行 chokepoint |
| D10 | VERIFIED | `runtime.fileops`：内部 artifact root source 由 typed Port/composition 保证 |
| D11 | VERIFIED | v2 manifest 验证 consumer/test evidence 关联并拒绝不相关 evidence |
| D12 | VERIFIED | `contracts.conversation.queue`：governed public surface 全部显式注解 |
| D13 | VERIFIED | `runtime.tools.capability_types`：正式 capability 使用精确 Protocol/Callable 签名 |
| D14 | VERIFIED | event/session/public wire owners：显式字段 encoder 与 exact decoder |
| D15 | VERIFIED | `Stage[T]`、deferred result/poll/executor 与 engine/definition 保留泛型关系 |
| D16 | VERIFIED | `contracts.tool.arguments`：deep-frozen `ToolArguments` 贯穿 policy/effect/execution identity |
| D17 | VERIFIED | 各 domain event/handoff owner：有限状态使用 enum/tagged union |
| D18 | VERIFIED | `runtime.events.telemetry`：owner-private nominal token 与 exact-type dispatch，包含 subclass 负例 |
| D19 | VERIFIED | `runtime.session`：JSON 只停留在 strict codec，事件不再自行 coercion |
| D20 | VERIFIED | clock 签名已修正；ledger 已指向直接覆盖 `CircuitBreaker` 的测试 |
| D21 | VERIFIED | `pyrightconfig.json`：stale/broad ignore 已退出，手写 RPC 进入全生产 Pyright |
| D22 | VERIFIED | `product.workflows`：resume transition 经 strict state validation |
| D23 | VERIFIED | ErrorReport/Message canonical codec；pickle、`__dict__.update`、`SerializeAsAny` 已退出 |
| D25 | VERIFIED | SQLite/session durable decoder：wrong primitive、missing/extra 字段 fail closed |
| D26 | VERIFIED | `orchestration.background_tasks`：foreground/background/hybrid tagged variants |
| D27 | VERIFIED | BackgroundTask Port/pool/result/notification 全链保持 `TaskId`/`AttemptId` |
| D28 | VERIFIED | Tool catalog admission deep-freeze，query 返回 immutable projection |
| D29 | VERIFIED | 与 D8 同切片删除 background setattr/getattr marker 路径 |
| D31 | VERIFIED | RunGraph strict state/channel schema、assignment validation 与 typed node failure |
| D32 | VERIFIED | generated protobuf 精确隔离，手写 wrapper 静态验证 |
| D34 | VERIFIED | Workflow/Tool durable identity 使用显式稳定 implementation/source identity |
| D35 | VERIFIED | deferred metadata 与 Product inspection 使用 immutable typed projection，无 live graph/state 泄漏 |
| D36 | VERIFIED | Tool/Lifecycle/PeriodicLoop/DynamicToolset 在执行前归一 sync/async；仅两个外部 SDK adapter 登记 probing |
| D37 | VERIFIED | inference/webhook aiohttp `AppKey[T]` 绑定 authoritative value type |
| D38 | VERIFIED | `ComponentKey[T]` 使用 owner-created nominal token 控制 slot 注册与 retrieval |

## 6. 验证记录

以下结果均绑定 2026-08-04 当前工作树。针对本次 5 项复验仅运行窄范围 Pyright、静态门禁和
单文件/单节点测试；遵照资源约束未运行全仓或大规模测试：

| 验证 | 实际结果 |
|---|---|
| `python -B -m ztest.architecture.static_governance confirmed-dynamic-debt-symbols` | VERIFIED：empty baseline，invariant closed |
| `pyright contracts kernel runtime orchestration product engine.py` | VERIFIED：0 errors, 0 warnings, 0 informations |
| `python -B typecheck/verify_type_contract_cases.py` | VERIFIED：PASS |
| `python -B -m pytest ztest/architecture -q --tb=short -p no:cacheprovider` | VERIFIED：100% PASS |
| Workflow、RunGraph、BackgroundTask、ToolExecutor、Tool binding、Function/Dynamic Toolset、MCP、Residency、Telemetry 组合套件 | VERIFIED：100% PASS |
| `ztest/runtime/test_lifecycle.py` + `ztest/cli/serving/test_connection_scope.py` | VERIFIED：15 passed |
| `ztest/events/test_types.py` | VERIFIED：4 passed |
| `ztest/workflows/test_engine.py` | VERIFIED：21 passed |
| `ztest/workflows/test_product_adapter.py` | VERIFIED：14 passed |
| `ztest/architecture/test_deferred_result_projection.py` | VERIFIED：8 passed |
| `ztest/fileops/test_transactions.py` | PARTIAL：前 12 项通过；多进程 crash case 未在时限内退出，为避免 WSL 资源风险未重跑 |
| D3 登记 decoder malformed 测试节点 | VERIFIED：4 passed |
| D11 evidence linkage 正反例 | VERIFIED：2 passed |

本次未运行全仓测试。D24、D30、D33 仍是第 0 节列出的精确 `VALID_BY_DESIGN` 排除，
不属于被伪装为关闭的债务。
