# Mote 泛型与动态类型边界治理实施规格

状态：`AUDITED_DRAFT / REQUIRES_APPROVAL_BEFORE_IMPLEMENTATION`  
审计基线：2026-08-04 当前工作树  
适用范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/`  
事实优先级：当前任务决定 → `AGENTS.md` → 当前源码与架构门禁 → 测试 → 本文

> 本文是开工规格，不是完成证明。路径、symbol 和统计只描述审计基线；每个工作包开工前必须重新检查工作区与消费者。关键词命中只是候选，只有破坏 authoritative type、泛型关系、严格解码、最小服务面、owner 或分层边界的用法才是债务。

范围排除：`ztest/`、`typecheck/cases/` 和其他测试 fixture 不进入债务 baseline，不要求清理其中的 `Any`、裸容器、assert、ignore、反射或非法输入构造。测试只用于证明生产 contract、门禁和负例；本专项不得为了测试源码整洁修改测试。治理基础设施自身（scanner、verifier、baseline/registry parser）属于工具正确性范围，但不把测试业务 fixture 当生产债务。

## 1. 审核结论

当前代码尚未闭合泛型与动态类型边界。本文给出第 8 节建议顺序，但当前任务只授权审核与文档修改，尚未批准生产实施；任何工作包进入 `IN_PROGRESS` 前仍需用户确认实施范围。原方案中的主要歧义已在本版收敛：

1. 将“扫描器、债务 baseline、批准保留的动态边界”拆成三个不同概念，禁止用 allowlist 隐藏债务；
2. 按真实 owner 拆分 Contracts 工作，不再把整个 W1 错设为所有后续工作的统一前置；
3. 将 Gateway 的真实实现对象、Workflow 的双生命周期、Tool 的运行时 chokepoint 写明；
4. 为每个工作包给出依赖、改动闭包、退出条件、验证与禁止事项；
5. 将一次性全仓 `strict` 改为按已治理 symbol 扩张的静态门禁，避免以无关存量阻塞垂直切片；
6. 明确本文不授权清空 durable 数据、引入依赖、建立兼容层或修改无关代码。

当前工作树有大量用户改动，且本文是未跟踪文件。实施者必须保留这些改动；每个切片只修改其确认过的文件。若相关文件在开工基线后继续变化，先重做该切片的消费者和 diff 审计。

## 2. 已核实的源码事实

### 2.1 现有治理基础设施

- `ztest/architecture/static_governance.py` 已拥有生产路径、AST、qualified name 和 CLI check 框架，应成为扫描规则的 canonical owner。
- `ztest/architecture/test_dynamic_boundary_governance.py` 当前复制了生产路径、qualified-name 和部分规则实现，而不是调用 `static_governance.py`；W0 必须先收敛这一重复事实，再扩展新规则。
- `typecheck/verify_type_contract_cases.py`、`typecheck/cases/{pass,fail}/` 和 `typecheck/case-expectations.json` 是正负类型契约的唯一执行链。2026-08-04 实际运行 verifier 通过；问题是 verifier 的动态 JSON 读取在全仓 Pyright 下自身报类型错误，不是运行时诊断比对失败。
- `typecheck/dynamic-boundaries.json` 当前有四条声明记录，字段主要为 name/file/source/validation/owner/review_after，但仓内没有门禁读取它，也没有精确 symbol、category 或 validation test identity。它目前是未执行的声明文件，尚不能证明边界已批准或仍有效。
- `pyrightconfig.json` 当前为 `basic`。治理必须以独立配置或精确执行范围渐进收紧，不能先把全仓切到 `strict`。

### 2.2 已沿消费者确认的债务

| ID | canonical owner / 当前位置 | 已核实问题 | 首个消费者闭包 |
|---|---|---|---|
| D0 | `runtime/models/failover/orchestrator.py` | provider 在 executor、selector、state、key、admission、observer 间退化为 `Any`；`_apply_decision` 又把 request transformer 退化为 `Any` | `runtime/models/model_gateway.py` 与 failover tests |
| D1 | `contracts/ports/model/gateway.py` 的实现链 | Port 已闭合 keyword 参数；`GenerationBoundRuntimeModelGateway`、`ExactCachedModelGateway`、`CurrentRuntimeModelGateway` 重新使用无注解 `**kwargs` | `runtime/models/{model_gateway,cached_gateway,composition_context}.py` 与 Product composition |
| D2 | `contracts/hook/invocation.py` | hook invocation 公开裸 `dict` | `runtime/hook/`、Product hook composition、wire tests |
| D3 | `contracts/events/` | 已沿 event codec、Runtime publisher 与 Product presentation 确认 telemetry/session/model 公开事件存在 `Any`、裸或开放 mapping 逃逸 | event codec、Runtime publishers、Product presentation |
| D4 | `contracts/conversation/messages.py` | canonical message、provider projection 与 persistence 的 `to_dict/from_dict` 生命周期混合 | Runtime session/history/compaction、model adapters |
| D5 | `contracts/model/profile.py` | `Callable[[dict], dict]` schema transformer 无生产消费者，形成未接入的开放扩展面 | 全生产读取点搜索；默认删除而非补造消费者 |
| D6 | `kernel/inference/tokenization.py`、`kernel/commands/` | Kernel 仍解释动态 message/command mapping | prompt/model adapter、native/XML command tests |
| D7 | `orchestration/workflows/` | 宽 callable/type/BaseNode、动态 state 与 process-local/durable definition 混合 | Product run graph、durability/recovery |
| D8 | `orchestration/background_tasks/decorators.py` | 私有 marker 与 `getattr` 发现 capability；decorator 未保留 callable shape | Product publication、Role/tool execution |
| D9 | `runtime/tools/tool_pipeline.py` | `ToolExecution.tool: Any`、宽 callable/args、signature probing 与 `getattr` 位于执行核心链 | registry/binding/executor、permission/effect/audit |
| D10 | `runtime/fileops/mutation/artifact_roots.py` | 已有 Protocol 仍以 `callable(getattr())` 验证内部 capability | artifact root composition、GC |
| D11 | 静态治理 | 当前门禁覆盖不足，门禁通过不能证明上述边界闭合 | governance runner、baseline、type verifier |

以下是 D12-D38 逐 symbol 生产消费者复核后的分类结果。`CONFIRMED_DEBT` 表示至少存在下方列明的真实债务 symbol；`VALID_BY_DESIGN` 只批准本次列明的当前命中，不构成对未来同关键词用法的通配豁免：

| ID | 候选族 | 当前状态 |
|---|---|---|
| D12 | public surface 隐式 Any | CONFIRMED_DEBT |
| D13 | 裸/ellipsis Callable 与宽 TypeAlias | CONFIRMED_DEBT |
| D14 | 自动对象投影进入 event/wire/durable | CONFIRMED_DEBT |
| D15 | factory/registry/collection 泛型实参丢失 | CONFIRMED_DEBT |
| D16 | 参数化宽 Mapping 隐藏已知 shape | CONFIRMED_DEBT |
| D17 | 开放字符串承担闭集 state/discriminator | CONFIRMED_DEBT |
| D18 | ignore、runtime Protocol、TypeGuard/introspection 伪造证明 | CONFIRMED_DEBT |
| D19 | parser/SDK/protobuf 动态结果逃逸 adapter | CONFIRMED_DEBT |
| D20 | overload/override/decorator/default 类型退化 | CONFIRMED_DEBT |
| D21 | stale/broad Pyright ignore | CONFIRMED_DEBT |
| D22 | unchecked Pydantic construction/validation bypass | CONFIRMED_DEBT |
| D23 | Python object/subclass shape 跨序列化或消息边界 | CONFIRMED_DEBT |
| D24 | assert 承担外部、安全或 durable 验证 | VALID_BY_DESIGN |
| D25 | decoder coercion/default/fail-open | CONFIRMED_DEBT |
| D26 | Optional/boolean 组合表达互斥 variant | CONFIRMED_DEBT |
| D27 | canonical identity/revision/fence 退化为裸 primitive | CONFIRMED_DEBT |
| D28 | frozen DTO 浅冻结或 mutable alias 泄漏 | CONFIRMED_DEBT |
| D29 | setattr/marker 动态注册 capability/state | CONFIRMED_DEBT |
| D30 | lambda/partial 擦除 callback 类型或 identity | VALID_BY_DESIGN |
| D31 | RunGraph 动态 expression 结果进入 Workflow state | CONFIRMED_DEBT |
| D32 | generated protobuf 豁免扩大到手写 RPC 代码 | CONFIRMED_DEBT |
| D33 | id/hash 被提升为稳定或跨 owner identity | VALID_BY_DESIGN |
| D34 | class name/qualname/repr 成为 domain identity/payload | CONFIRMED_DEBT |
| D35 | callback 注入完整 Role/State/Context/Services | CONFIRMED_DEBT |
| D36 | sync/async callback lifecycle 混合并运行时猜测 | CONFIRMED_DEBT |
| D37 | typed key/value 关联被 object/Any + cast 擦除 | CONFIRMED_DEBT |
| D38 | typed token 仅靠可伪造 primitive identity 承诺泛型 | CONFIRMED_DEBT |

D0-D23、D25-D29、D31-D32、D34-D38 是当前已确认债务；D24、D30、D33 的当前生产命中经消费者核实为有意设计。不存在未核实候选。

逐 symbol 债务证据：

| ID | confirmed symbol / data flow | 判定依据 |
|---|---|---|
| D12 | `contracts/conversation/queue.py::MessageQueue.push/empty` | public Contract 方法缺返回注解，经 Role/message queue 消费；隐式 Any 已越过 bounded-context 服务面，不是 framework 固定 hook |
| D13 | `runtime/tools/capability_types.py::{HandoffRuntime,WaitInterruptible,RunSkillFork,CommitGraphOutput,ResumeGraphOutput,DescribeImage}` | 均为 Runtime 内部正式 capability，真实 keyword 参数已知且由 Role/tool consumer 构造调用，ellipsis 擦除参数关系；graph output 两项还以动态 await result 穿过 tool boundary |
| D14 | `contracts/events/_base.py::Event.payload`、`runtime/session/events.py::SessionMetaEvent.payload`、`product/interfaces/inference_api/model_operations.py` artifact projection | `vars/asdict/model_dump` 结果直接成为 event、session durable payload 或 public wire；新增内部字段会自动外泄，消费者没有独立显式 encoder |
| D15 | `orchestration/workflows/base_node.py::{_resolve_type,derive_reducers}` 与 `runtime/tools/provider.py` definition aliases | process-local Workflow 注册/执行用裸 `type`、裸 `Callable`，tool definition registry 用 `Any` 汇总实参；消费者继续 introspect/cast，泛型关系未封装在 typed binding 内 |
| D16 | `contracts/tool/actions.py::ToolCallAction.arguments`、`contracts/tool/policy.py` arguments 与 Runtime ToolExecutor 链 | 参数虽然是 mapping，但实际受 tool definition schema、permission targets 和 effect digest 解释；`dict[str, Any]` 穿过 Contract、policy、execution，不是无人读取的开放 JSON |
| D17 | `contracts/events/telemetry.py::{RecoveryEvent.phase,SpanEndEvent.status}`、`contracts/runtime/handoff.py::RuntimeHandoffResolution.status` | 注释和状态机消费者明确给出有限合法值，但 Contract 接受任意字符串；错误值可进入 telemetry/durable handoff projection |
| D18 | `runtime/events/telemetry.py::_TypedTelemetryBinding.erase` | 对 handler、sync handler、event type 分别 cast 到 object 后进入异构 manifest；运行时只按 event type 分派，现有 registry 声称 TypeGuard 验证但源码没有该证明 |
| D19 | `runtime/session/events.py::{ContextCompactedFact,HistoryEditedFact,RoutingDecisionFact}.from_payload` | durable session replay 输入为 `Dict[str, Any]`，字段通过 `str/int` 恢复；动态 JSON shape 离开 parse adapter并由多个 event class 再解释 |
| D20 | `runtime/resilience/breaker.py::CircuitBreaker.__init__.clock` | 参数声明 `Callable[[], float]` 却以 `None` 为默认并用 ignore 消音；真实 implementation signature 超出注解值域，消费者无法静态区分默认 clock |
| D21 | `pyrightconfig.json::ignore` | 5 个条目指向已不存在路径；`product/inference/daemon/rpc` 是目录级 ignore，同时覆盖 generated protobuf 与手写 RPC wrapper，生产静态验证被整体绕过 |
| D22 | `product/workflows/agent_service.py::resume_plan` | durable checkpoint state 只校验 override key 是否存在，随后 `model_copy(update=plan.overrides)` 绕过字段 validation；错误 primitive/variant 可进入恢复后的 Workflow state |
| D23 | `contracts/foundation/errors/base.py::_rebuild_mote_error`、`contracts/conversation/{queue,messages}.py` | Contract 通过 pickle、`__new__ + __dict__.update`、`SerializeAsAny` 保存 Python subclass/object shape；这些对象随后进入 session/message serialization，而非仅 logging adapter |
| D31 | `product/workflows/run_graph/compiler.py::_build_state_schema`、`resolve_binding` 与 node commit 链 | 源码明确声明 channel type 仅 advisory；`GraphState` 使用 `extra="allow"`、无 assignment validation，provided input/node result 不校验就进入统一 Workflow state |
| D25 | `product/inference/backends/sqlite.py::_metadata_from_json`、`runtime/session/events.py` 多个 `from_payload` | durable metadata/session event decoder 对字段使用 `str/int` 构造器；错误 primitive 会被修复成合法值而非 fail closed |
| D26 | `orchestration/background_tasks/model.py::BgTaskResult` | `mode`、`result`、`poll_factory` 可直接构造出 foreground-with-poll、background-with-result 或 background-without-poll；named constructor 只是约定，没有阻止非法 variant |
| D27 | `contracts/ports/task/operations.py` 与 `orchestration/background_tasks/model.py::{TaskSnapshot,_TaskState}` | 已存在 `TaskId`/`AttemptId`，但跨 Pool query/status/result surface 的 task identity 仍退化为 `str`，与 Agent/process/incarnation ownership 关系被切断 |
| D28 | `contracts/tool/catalog.py::MaterializedToolDefinition.input_schema` | frozen DTO 接受并原样保存调用方 dict，只将注解写成 `Mapping`；catalog fingerprint/permission consumer 可在 admission 后观察到外部 mutation |
| D29 | `orchestration/background_tasks/decorators.py::{background,is_bg_tool}` | decorator 通过 marker attribute/setattr 注册 execution disposition，Product/tool publication 再以 getattr 发现，形成跨 owner 动态 capability |
| D32 | `pyrightconfig.json` 与 `product/inference/daemon/rpc/` | 同 D21 的目录 ignore 不仅覆盖 generator-owned `*_pb2*.py`，也覆盖手写 client/server glue；生成代码 typing 缺口扩大成生产目录豁免 |
| D34 | `orchestration/workflows/definition.py::{_type_identity,_code_payload}`、`runtime/tools/definition_compiler.py` | module/qualname/class name 参与 Workflow definition digest 和 tool semantic identity，class/module rename 会改变可恢复或审计 identity，而非仅诊断文本 |
| D35 | `product/workflows/run_graph/compiler.py::_make_router` 与 node callable、`runtime/agent/components/context.py` extension callbacks | router/node 获得完整可变 `GraphState`；部分跨组件 callback 闭包完整 Role 后只读取少量字段，能力面大于真实 consumer requirement |
| D36 | `product/presentation/consumer.py::BaseConsumer.handle_sync` | 先调用 handler，返回 coroutine 后才 `close()`；sync/async lifecycle 在执行后猜测，handler 调用前副作用及 coroutine cleanup 没有单一 contract |
| D37 | `product/interfaces/inference_api/application.py` 的 9 个 `AppKey`、`product/interfaces/inference_webhook_api/application.py` 的 2 个 `AppKey` | Gateway、Authorizer、Route、owner、reader、verifier、sink 均为已知类型，却统一存为 `object`，每个消费者再 cast 恢复，composition 无法静态拒绝错配 |
| D38 | `runtime/agent/component_graph.py::ComponentKey`、`ComponentGraph.get` | token 只保存公开 `name: str`，spec/slot 同样按 name 索引；调用方可用同名错误 `ComponentKey[T]` 命中值，随后由 unchecked cast 兑现伪造的 `T` |

`VALID_BY_DESIGN` 核实证据：

| ID | 已核实生产命中 | 保留理由 |
|---|---|---|
| D24 | Contracts/Runtime codec 中的 `assert isinstance`；writer、scheduler、pool、sandbox lifecycle 中的 non-None assert | codec assert 均紧跟同函数内 `type(...) is ...` fail-closed 分支，仅帮助类型检查器；其余 assert 表达 owner 锁/状态机保护后的内部不可达状态，没有替代外部输入、permission、fence 或 durable shape 验证 |
| D30 | 当前生产 lambda/partial inventory | 命中均属于 dataclass/default factory、排序 key、局部 process callback，或传入已有完整类型签名的 constructor 参数；没有 lambda/partial 被持久化为 definition identity，也没有借此扩大公开 callback contract。闭包持有完整 Role 的问题单独计入 D35 |
| D33 | 生产内建 `id()` inventory | Runtime engine/tool lifecycle/catalog、model resource cleanup、compaction reducer 均用于同一进程对象去重；ContextVar 名和 presentation lifecycle label 仅作进程内诊断。生产 failover 调用显式传入稳定 endpoint key，未把默认 `id` 写入 resume/durable facts |

## 3. 判债与保留规则

### 3.1 必须整改

满足任一条件即进入债务 baseline：

- 位于 Contract、Port、public service、composition、durable schema、event、effect 或 permission 边界；
- 可表达的 input/output、provider/selector、callable/wrapper、request/result 关系被 `Any`、`object`、裸容器或裸 `Callable` 切断；
- 已知 key、必填字段、互斥 variant、version 或 discriminator 仍由 mapping 表达；
- `dict[str, object]` 离开 decoder 后被按业务 key 解释；
- `getattr/hasattr/callable` 用于发现内部 capability、lifecycle 或 owner；
- `**kwargs` 扩大 Protocol 参数闭集；
- `cast` 的不变量未在同一入口先行验证；
- 动态值进入 durable、workflow、agent、tool、permission 或 effect 真相链。
- public Contract、Port、service、factory、registry、catalog、callback 或其生产实现缺参数/返回注解；
- `Callable[..., R]` 或宽 `TypeAlias` 隐藏本可由 `ParamSpec`/`TypeVar` 表达的内部 capability；
- `model_dump/asdict/vars/__dict__` 的自动结果未经显式 encoder 就越过 event、wire 或 durable 边界；
- factory/collection 返回缺少泛型实参的基类，或可变 collection 被错误当作协变；
- `type`、`Coroutine`、`Awaitable`、`Iterator`、`Generator` 等泛型缺少实参，使 class instance、yield/send/return 或 await result 退化；
- `Mapping/Sequence/TypedDict` 的元素 shape 已知，却只把裸容器换了名称；
- 已知闭集的 `kind/status/type/operation/phase` 仍为普通 `str`；
- `runtime_checkable Protocol`、TypeGuard、dynamic schema 或 type ignore 被用于证明运行时无法验证的泛型实参或正式 API 兼容性。
- `json.loads/yaml.safe_load`、第三方 SDK、protobuf 或 untyped import 的返回未在 adapter 入口收窄并严格解码，导致 `Any/Unknown` 逃逸；
- overload implementation、subclass override 或 decorator wrapper 的真实签名比声明/Port 更宽，或丢失泛型关系；
- Pyright ignore/exclude 指向失效路径、覆盖整个生产目录，或使 governed symbol 不参与静态验证。
- `model_copy(update=...)`、`model_construct`、宽松 `model_validate`、`extra="allow"` 或 `arbitrary_types_allowed` 用于 durable/security/identity/state transition，且没有同处严格验证；
- pickle/dill/cloudpickle/marshal、`SerializeAsAny`、`__new__ + __dict__.update` 或 `list[object]` 让 Python class/object shape 成为跨进程、durable、agent message 或 public contract。
- `assert` 承担外部输入、durable record、permission、identity、fence 或 state transition 的 fail-closed 验证；
- decoder 对未验证值做 `str/int/bool/float` 强转、以 `.get(default)` 接受缺字段，或捕获解析错误后返回正常默认状态。
- 多个 Optional/boolean 字段实际表达互斥 lifecycle/result/disposition，却没有 tagged union，允许同时缺失或冲突组合；
- 同一 Agent/Task/Run/Effect/Lease/Fence/Revision identity 已有 canonical type，却在 Port、callback、handle、collection 或 service 中退化为裸 `str/int`。
- frozen DTO 只做浅冻结、字段仍可原地修改，或 query/property/callback 将 owner 的内部 mutable collection 引用返回给调用方。
- 通用 `setattr`、marker attribute 或 class mutation 注册内部 capability/definition，或按字符串字段直接修改另一个 owner 的 state；
- lambda/partial 被注册为 public callback、hook、tool、workflow/reducer/lifecycle capability，且其参数/结果关系或稳定 implementation identity 无法静态证明。
- expression/template/script evaluator 的动态结果未经 declared input/output/channel contract 验证就进入 Workflow、tool result 或 durable state；
- 以生成代码动态或 typing 不完整为由忽略包含手写 wrapper 的整个生产目录。
- `id()`、随机化内建 `hash()` 或对象地址被用于 durable、跨进程、event、receipt、subscription 或可恢复 identity；
- class/module qualname、`type(...).__name__` 或 `repr()` 被用作 domain discriminator、error code、definition identity 或 canonical payload，而不是纯诊断文本。
- 跨包、extension、policy 或 callback 边界接收完整 Role/State/Context/Services/manager，而真实消费者只需要其中少量 query/command，或因此获得扩大权限/预算/工具的能力。

### 3.2 可以保留的真实动态边界

仅允许：第三方/平台对象 adapter、JSON parser 到严格 decoder 的瞬时值、Pydantic `mode="before"` 输入、平台常量兼容、第三方异常投影，以及经产品决定允许的开放 JSON。

每项必须登记在 `typecheck/dynamic-boundaries.json`，具备精确文件与 symbol、外部 source、同处 validation、owner、review_after 和边界测试。登记册只能包含长期真实边界，不得包含“稍后清理”的内部债务。

Product-owned definition compiler 可以对显式批准的 callable 做一次性 `inspect.signature/get_type_hints`，但必须输出带稳定 identity/version 的不可变 definition；Runtime/Orchestration 执行与恢复不得再次 introspect callable。Pydantic validator 的动态输入必须显式注解为 `object` 或 canonical JSON input 并立即验证，不能靠缺注解获得兼容性。

### 3.3 三类治理数据不得混合

| 数据 | owner | 内容 | 退出方式 |
|---|---|---|---|
| 扫描规则 | `ztest/architecture/static_governance.py` | 类别定义、AST 语义、稳定诊断 | 规则版本演进 |
| 债务 baseline | `typecheck/dynamic-type-debt.json`（W0 新增） | 精确 path + qualified symbol + category + owner + target work package | 只能随整改删除；新增失败 |
| 合法动态边界登记 | `typecheck/dynamic-boundaries.json` | 外部 source + 入口验证 + owner + review | 定期复审或边界消失后删除 |

baseline 不保存行号（行号不稳定），但诊断必须输出行号；identity 使用 `path + qualified symbol + category`。模块级注解使用明确的 pseudo-symbol。禁止目录级、文件级通配豁免。

`typecheck/dynamic-type-debt.json` 使用版本化 envelope，至少包含：

```text
schema_version
generated_against.source_roots
generated_against.ruleset_version
records[]:
  path
  qualified_symbol
  category
  layer
  canonical_owner
  work_package
  consumer_evidence[]
  invariant_broken
  target_replacement
  admitted_on
```

扫描器输出 candidate inventory；只有人工核实 owner、消费者和破坏的不变量后，记录才可进入 debt baseline。合法边界直接进入升级后的 `dynamic-boundaries.json`，不得先进入债务再以永久 waiver 留存。现有四条文件级声明必须在 W0 逐条重新核实，不能自动视为已批准。文件移动或 symbol 重命名必须以同一切片的显式 old-identity removal + new-identity admission/closure 处理，扫描器不得按相似名称自动迁移记录。源码 hash 可作为执行证据，但不能替代 symbol identity、consumer evidence 或人工判定。

### 3.4 现有动态边界登记的预审

| 现有 name | 当前预审事实 | W0 处置 |
|---|---|---|
| `heterogeneous-telemetry-core` | validation 声称使用 TypeGuard，但当前 `runtime/events/telemetry.py` 的 typed binding erase 使用 `cast`，登记描述已与源码漂移 | 标记 stale，重新证明 heterogeneous erasure 是否类型安全；不得自动迁移批准 |
| `heterogeneous-tool-definition-registry` | `runtime/tools/provider.py` 仍有 `Xml/NativeToolDefinition[Any]` 与 `Mapping[str, Any]` policy alias，但登记没有精确 symbol | 该登记不进入债务清单；拆到具体 symbol 并沿消费者确认，或以真实 adapter 证据重新准入 |
| `heterogeneous-role-component-registry` | `ComponentKey[T]`、`ComponentSpec[...]` 保留静态关系，但 heterogeneous slot erasure 的具体 symbol/验证测试未登记 | 核实 key-controlled erasure 和所有 cast；只有无法避免且不伪造 runtime 泛型时才重新准入 |
| `open-view-event-consumer-dispatch` | 登记声称 reflective `on_<kind>` lookup；当前 `BaseConsumer._handler_for` 默认返回 `None` 并要求 closed table override，登记描述已漂移 | 若全生产无反射 dispatch，删除 stale 记录；若 subclass 仍反射，先精确到 symbol 并沿消费者确认，不预设为债务 |

该预审不是批准或最终债务判定，只证明现有登记不能作为当前关闭证据。

## 4. 目标架构

### 4.1 泛型关系

真实关系必须从声明到结果保持同一类型变量：

```text
ProviderT + RequestT + ResultT
  -> AttemptState[ProviderT, RequestT]
  -> AttemptExecutor[ProviderT, RequestT, ResultT]
  -> ProviderSelector[ProviderT]
  -> RequestTransformer[ProviderT, RequestT]
  -> DecisionObserver[ProviderT]
  -> ResultT
```

Decorator 使用 `ParamSpec` 和 `TypeVar`；sync/async 同时存在时用 overload 或两个明确入口，不能用一个宽 wrapper 猜测并无条件 `await`。

Provider key 若只需稳定 hash/equality，先复用已有 identity contract；确认没有合适类型后，才在模型 failover owner 中定义不可变、准确的 key 类型。不得用 `object` 伪装未设计的 identity。

### 4.2 Mapping 的合法角色

| 角色 | 类型 | 生命周期 |
|---|---|---|
| 未验证外部对象 | `object` / `Mapping[str, object]` | decoder 入口内 |
| 明确开放 JSON | 仓内 canonical `JsonValue` / `JsonObject` | 经批准的 JSON 边界 |
| 已知业务 shape | dataclass/Pydantic DTO/tagged union | Contract、Port、service、state |
| provider wire | provider-owned typed projection/局部 wire alias | Product adapter 内，发送后不回流核心 |

Codec 的 domain 输入输出必须是 canonical DTO。Durable/wire decoder 必须拒绝未知 version/tag、额外关键字段、缺字段、`bool` 冒充 `int` 等错误 primitive。

### 4.3 Capability 与服务面

内部 capability 只能通过 immutable definition 的 typed disposition、Contracts-owned 窄 Port、Product manifest/catalog 的稳定 identity 或 tagged union 显式装配。Tool pipeline 只能取得 immutable binding snapshot 与 typed command/result，不能取得 live registry、完整 Role 或动态 tool instance。

### 4.4 Governed symbol 的选择

`public` 不能简单定义为“名称不以下划线开头”。Governed symbol 至少包括：

- Contracts 导出的 DTO、enum、tagged union、TypeAlias、Protocol 及其成员；
- durable/wire/event/effect/permission 的 encoder、decoder、record 和 transition；
- composition root、factory、builder、registry/catalog command/query 与 Protocol 实现；
- 被注册为 callback、hook、tool、workflow node、reducer、subscriber 或 lifecycle handler 的 callable；
- 跨 bounded context 调用的 service 方法，即使当前名称是 private；
- 参与泛型 definition -> builder -> runtime -> handle -> result 链的中间类型；
- 承担 activation、shutdown、recovery、resume、migration 或 fenced commit 的入口。

以下内容不会仅因名称公开自动成为 governed symbol：纯 presentation formatter、无跨界状态的局部算法、测试 helper、第三方 framework 要求的薄 adapter。反之，private codec、callback 和 state mutation 只要承担上述正式语义就必须治理。Governed-symbol inventory 必须保存选择理由和 consumer evidence，不能只保存路径列表。

### 4.5 风险优先级

| 级别 | 判定 | 处置 |
|---|---|---|
| P0 | 动态值进入 durable、permission、effect、fence、identity、recovery，或泛型错误可导致错误 owner/result | 在所属前置 contract 稳定后优先实施；不得登记为临时合法边界 |
| P1 | Contract/Port/composition/Workflow/Tool service 面类型擦除，但尚无持久化或安全后果 | 按第 8 节 owner 顺序实施 |
| P2 | Product adapter、presentation、第三方 SDK typing 缺口，且已在入口验证并不回流核心 | 可登记合法边界并设置 review_after，或在 W6B 收敛 |

风险级别不能改变依赖方向或授权破坏 durable 数据。P0 只提高已确认工作包的调度优先级，不允许跳过 canonical Contract、migration 决定和消费者闭包。

## 5. W0：建立可信增量门禁

依赖：无。此包是所有生产整改包的前置。

改动闭包：

1. 在 `static_governance.py` 扩展一个 `dynamic-types` check；先把 `test_dynamic_boundary_governance.py` 中重复的生产路径、qualified-name 和规则逻辑改为调用同一实现，pytest 不再维护第二套扫描器。
2. 第一版只实现语义稳定的类别：`BARE_DICT_ANNOTATION`、`BARE_CALLABLE_ANNOTATION`、`UNTYPED_KWARGS`、`CAST_ANY`、`DYNAMIC_DUNDER_GETATTR`、`LOCAL_IMPORT`。`ANY_IN_BOUNDARY` 与 `INTERNAL_CAPABILITY_REFLECTION` 需要 governed-symbol/显式登记语义，不能把所有 `Any/getattr` 直接判错。
3. 生成并人工审阅 `typecheck/dynamic-type-debt.json`。每项必须绑定 owner 与 D0-D11 或后续编号；扫描器不得自动把命中写成批准状态。
4. 新命中失败；baseline identity 消失时报 stale baseline 失败；同一 identity 类别变化也失败。
5. 对新建或已经完成治理的 symbol 使用零容忍 governed-symbol 集合。不能把已有存量的整个 `contracts/ports/` 或 composition 目录立即设为零容忍。
6. 保持当前 type-contract verifier 的运行语义和精确 file/rule/line 比对；为 JSON 输出定义严格内部 DTO/decoder，修复 verifier 自身的 Pyright 错误，并增加 malformed/missing diagnostic JSON 自测。
7. 第二批语义规则覆盖 `IMPLICIT_ANY_PUBLIC_SURFACE`、`ELLIPSIS_CALLABLE_CAPABILITY`、`WIDE_TYPE_ALIAS`、`AUTOMATIC_BOUNDARY_PROJECTION`、`MISSING_GENERIC_ARGUMENT`、`WIDE_MAPPING_BOUNDARY`、`OPEN_STRING_STATE`、`UNSCOPED_TYPE_IGNORE` 和 `GENERIC_RUNTIME_PROTOCOL_PROOF`。`MISSING_GENERIC_ARGUMENT` 同时检查容器、`type`、async iterator/generator、`Coroutine` 和 `Awaitable`，不能只检查 `dict/list`。无法纯 AST 判定的类别必须使用 governed-symbol 与人工审阅 baseline，不得按关键词全仓判错。
8. 为 candidate inventory、人工 debt admission、合法边界 admission、stale identity、symbol move 和 ruleset version mismatch 分别提供 fixture；门禁只读 baseline，不得在测试中自动接受或重写记录。
9. 升级并接通 `dynamic-boundaries.json`：每条记录增加 qualified symbol、category、validation test identity 和审批/复核状态；检查重复 identity、未知字段、过期 review_after、目标 symbol/validation test 消失。现有四条记录必须逐条重新准入，不能由 schema migration 自动批准。

W0 不做：批量改生产注解、把债务移入合法边界登记、复制 `production_paths()`、全仓开启 strict。

完成条件：fixture 对六个稳定类别均有正/负例；新增、stale、登记边界三种路径均被测试；CLI、pytest 和 verifier 通过。

### 5.1 规则精度合同

| 类别 | 自动发现 | 必须排除/人工判定 |
|---|---|---|
| `BARE_*_ANNOTATION` | annotation AST 中未带实参的内建/typing 泛型 | 普通构造调用、docstring、字符串内容；forward annotation 仍需解析 |
| `UNTYPED_KWARGS` | callable 的 `kwarg.annotation is None` | 不因 `**kwargs: Any` 而通过；后者进入 governed boundary Any 规则 |
| `IMPLICIT_ANY_PUBLIC_SURFACE` | governed callable 缺参数/返回注解 | `self/cls`、合法 framework hook；合法 hook 必须登记而非静默跳过 |
| `ELLIPSIS_CALLABLE_CAPABILITY` | governed alias/field 使用 `Callable[..., R]` | 外部框架完全拥有调用参数的 adapter，经登记后可保留 |
| `AUTOMATIC_BOUNDARY_PROJECTION` | `vars/__dict__/asdict/model_dump/SimpleNamespace` 的数据流到边界 sink | owner 内 debug/presentation 临时投影；必须基于消费者而非单次调用判错 |
| `UNTYPED_EXTERNAL_RESULT_ESCAPE` | JSON/YAML/SDK/protobuf/untyped import 结果离开 adapter 或被按 key/attribute 读取 | 同函数立即 strict decode 为 canonical DTO 的入口 |
| `OVERLOAD_OVERRIDE_ERASURE` | governed overload implementation/override/wrapper 缺注解、扩大参数或擦除实参 | 框架固定签名 adapter；仍须显式注解并登记 |
| `ANNOTATION_DEFAULT_MISMATCH` | 参数/字段默认值不属于声明类型，例如非 Optional callable 默认 `None` | sentinel 已由 tagged union/overload 明确表达的构造入口 |
| `STALE_OR_BROAD_TYPECHECK_IGNORE` | 配置 ignore 目标不存在、为目录或覆盖 governed symbol | 无；真实第三方缺口使用最窄 module/symbol adapter 与登记 |
| `VALIDATION_BYPASS_CONSTRUCTION` | governed model 使用 unchecked update/construct、open extras、arbitrary types 或非 strict 外部 validation | owner 内对已验证同类型值的 deep copy；必须证明 update 不引入新值 |
| `PYTHON_OBJECT_BOUNDARY` | pickle/SerializeAsAny/arbitrary object/`__dict__` restore 越过进程、durable、message 或 Contract | 纯 process-local 第三方 logging adapter，且 payload 不成为 canonical fact |
| `ASSERT_AS_BOUNDARY_VALIDATION` | assert 的条件读取外部/durable/security/identity/state 数据 | 纯内部不可达状态的开发断言；不能承担公开保证 |
| `COERCIVE_OR_DEFAULTING_DECODER` | decoder 对 raw value 强转、默认缺字段或吞解析错误 | encoder 对已验证 canonical field 的格式转换、内部数值计算 |
| `UNTAGGED_OPTIONAL_STATE` | 多字段组合决定 variant/terminal outcome/lifecycle，且非法组合可构造 | 彼此独立的 optional config、telemetry correlation 或展示字段 |
| `PRIMITIVE_IDENTITY_ERASURE` | governed 边界中命名型 primitive 对应已有 canonical identity/revision/fence type | 最外层 wire encode/decode、真正无 domain identity 的局部计数/文本 |
| `MUTABLE_STATE_ALIAS_ESCAPE` | frozen/public DTO 含 mutable field，或返回表达式泄漏 owner 私有 mutable collection | owner 内局部 mutation、返回新 copy 但 contract 明确不承诺 live view |
| `DYNAMIC_DECLARATION_OR_STATE_MUTATION` | 精确 built-in `setattr`/marker/class mutation 控制 capability、definition 或跨 owner state | `termios.tcsetattr` 等不同 API；owner 内对已声明固定字段的局部赋值不需反射 |
| `ANONYMOUS_CALLBACK_ERASURE` | lambda/partial 进入 governed callback/registry/catalog/composition | dataclass default_factory、局部 key function、已由完整 typed target 约束的纯 partial |
| `DYNAMIC_EXPRESSION_RESULT_ESCAPE` | eval/interpreter/template result 进入 governed state/result/effect | Product adapter 内产生后立即按批准 definition 验证的值 |
| `BROAD_GENERATED_CODE_EXEMPTION` | ignore/exclude 覆盖生成文件之外的手写生产代码 | 精确 generated-file manifest，且 typed wrapper 完整验证输入输出 |
| `PROCESS_LOCAL_IDENTITY_ESCAPE` | id/hash/address-derived key 离开当前 process-local collection/lifecycle | 同一调用或 incarnation 内 visited/dedupe set，值不持久化、不发事件、不跨 owner |
| `RUNTIME_NAME_AS_DOMAIN_IDENTITY` | class name/qualname/repr 驱动 codec、registry、definition、receipt 或 recovery | 日志、异常展示和 fail-closed unsupported-type message |
| `OVERBROAD_TYPED_CAPABILITY` | cross-package/extension callback 参数为完整 Role/State/Context/Services/manager | 同一 bounded context 内部实现协作，且对象不越过 public service 面 |
| `MIXED_SYNC_ASYNC_CALLBACK` | governed callback 返回 `R | Awaitable[R]`、`Any/object` 后以 awaitability probing 决定执行，或 sync path 创建后丢弃/关闭 coroutine | 明确的外部 framework adapter；必须在入口归一为单一 typed async binding，并登记异常、取消与 cleanup 语义 |
| `TYPED_KEY_VALUE_CORRELATION_ERASURE` | typed key、token 或 class key 被声明为 `Key[object/Any]`，随后按每个 key 的已知类型 cast；或异构 store 的 put/get API 未用同一 `TypeVar` 关联 key 与 value | 外部框架确实不提供泛型 key 的 adapter；必须在唯一入口验证并返回 typed composition snapshot，不能把 cast 扩散给消费者 |
| `FORGEABLE_PHANTOM_GENERIC_TOKEN` | `Key[T]`/handle/token 的 runtime identity 只是公开 name/id，任意调用方可用错误 `T` 重建等价 key，retrieval 再 cast 为所声称类型 | token 只在封闭 owner 内创建且无法越过该 owner；仍需 negative fixture 证明错误 `T` 无法构造或命中 |
| `OPEN_STRING_STATE` | governed DTO/command/event 中候选字段名与 `str` 注解 | 用户文本、presentation label、外部开放协议；必须证明合法值闭集 |
| `INTERNAL_CAPABILITY_REFLECTION` | 反射结果控制注册、分支、调用或 state mutation | 平台常量、第三方异常/SDK adapter；需同处 validation |
| `GENERIC_RUNTIME_PROTOCOL_PROOF` | runtime Protocol/TypeGuard/isinstance/cast 参与泛型收窄 | 非泛型最小 shape 检查；不得由运行时结果推断类型实参 |
| `UNSCOPED_TYPE_IGNORE` | 无精确错误码或位于 governed symbol 的 ignore | 测试构造非法输入；生产保留项仍需登记与 review_after |

扫描诊断必须给出 category、path、qualified symbol、line 和简短 evidence；不能在诊断中猜测 canonical replacement。语义类别的 candidate 只有人工确认后才进入 debt baseline。

## 6. 生产垂直切片

每个切片遵循：`inventory -> contract/owner -> implementation -> composition -> consumers -> retirement -> gates`。同一切片迁移全部仓内消费者并删除旧入口，不保留 alias、fallback、双读或双 API。

### W1A：模型 failover 泛型（D0）

依赖：W0。

- 泛型化 `AttemptOrchestrator`、`_ModelCallState` 和全部 provider callbacks；request/result/provider 关系端到端不退化。
- 核实 `AttemptResumeSeed.attempts_by_provider` 的 durable/进程内语义后决定 key contract；若跨进程，必须使用版本化稳定 identity，不能持久化 Python object identity。
- 迁移 `RuntimeModelGateway` 调用与 failover tests。
- 增加 provider/request/result mismatch fail fixtures 和 inference pass fixture。

完成条件：D0 baseline 清空；生产链无相关 `Any/object` 断点；failover 正常、切换、transform、resume 测试通过。

### W1B：ModelGateway 实现签名（D1）

依赖：W0；可在 W1A 后顺序执行，不依赖其 API 设计。

- `GenerationBoundRuntimeModelGateway`、`ExactCachedModelGateway`、`CurrentRuntimeModelGateway` 的 `execute/resume` 逐项复现 canonical Port 签名。
- Current proxy 只解析当前 gateway，不获得开放参数转发权；cached decorator 保留 request transformer、stream、session facts、artifact resolver 的语义。
- 增加静态 conformance pass fixture、未知 keyword fail fixture及 execute/resume 行为测试。

完成条件：D1 清空；三实现签名与 Port 一致；Product composition 仍只选择一个 gateway 链。

### W2A：Hook contracts（D2）

依赖：W0。

- 先列出 `contracts/hook/invocation.py` 每个字段的 Runtime、Product、wire 消费者。
- 复用 canonical tool arguments、identity、typed error/result；只有确认 shape 真正开放时才使用 canonical JSON。
- 同切片迁移 hook manager/subscriber、composition 和 wire codec，删除旧 dict 入口。

完成条件：D2 清空；malformed/extra/missing/wrong primitive fail closed；Hook 不能扩大 permission、arguments 或 capability。

### W2B：Event fields（D3）

依赖：W0。按 event family 分小提交，但一个 family 必须形成完整垂直切片。

- 逐字段分类为 domain DTO、批准开放 JSON 或外部 adapter；不得按文件批量替换。
- 每个 externally observable 变更同时处理 schema/version/codec/publisher/consumer。
- durable 或外部 wire 格式若变化，先选择保留、一次性 migration 或经用户授权丢弃；本专项默认无权删除。

完成条件：目标 family 的 debt 清空，严格 codec 与 presentation/event tests 通过；所有格式变化有明确 migration 决策。

### W2C：Conversation 生命周期拆分（D4）

依赖：W0；若触及模型 provider projection，应在 W1A/W1B 后执行。

- 保留唯一 canonical Message；provider projection 归 Product adapter，persisted envelope/codec 归拥有 durability 的 owner。
- 盘点并迁移 session history、compaction recovery、context reducer 和 provider consumers。
- `Message.to_dict/from_dict` 只有在能明确代表单一 canonical wire 生命周期时才保留，否则删除并由命名 codec 取代。

完成条件：不再由一个 mapping 同时承担 domain/provider/persistence；旧入口与 re-export 删除；session replay/compaction/model tests 通过。

### W2D：Model profile transformer（D5）

- 重新确认全生产无消费者后，删除字段、注释中的扩展承诺与死入口；不得为保留它而补造消费者。
- 若复查发现真实消费者，则改为 typed Port/DTO，并沿该消费者闭合 composition、失败和测试语义。

完成条件：不存在无消费者的开放 callback，也不存在为迁就旧字段制造的新执行路径。

### W3：Kernel command/message 边界（D6）

依赖：W0、W2C 的 canonical message 决定；若核实某子切片与 Message 无关，可记录证据后先做。

- 搜索并复用现有 canonical message、command、tool-call、stream event 和 output contract。
- tokenization 接受 canonical tokenizable projection；provider wire 只在 Product adapter 中存在。
- native/XML parse、partial stream、recovery 返回 canonical tagged union；Kernel public service 不解释 provider key。

完成条件：D6 清空；Kernel public service 无已知 shape 的裸 mapping；native/XML 正常、错误、partial、recovery tests 通过。

### W4A：Workflow 生命周期与 definition 边界（D7，第一步）

依赖：W0。必须先于 W4B。

- 明确区分 process-local callable graph 与 durable WorkflowRun definition。前者可以在内存持有 callable；后者绝不能持久化 callable、closure、Python object 或 code introspection payload。
- 盘点 `WorkflowBuilder`、`definition.py`、durable store/reconciliation 与 Product run graph 的 composition；记录唯一 durable definition identity 和 compiler owner。
- 若当前 durable definition 从 callable/code object 派生，先设计版本化、可恢复的 implementation identity/catalog 替代方案，再修改 API。

完成条件：形成源码内可执行的唯一 owner 链；durable schema 只引用批准 definition identity；无第二 workflow engine。若需要 durable migration，未获策略确认前本包标记 BLOCKED，不猜测。

### W4B：Workflow callable、reducer、parameter 类型（D7，第二步）

依赖：W4A。

- 为 process-local registration 定义 canonical typed command；decorator 只构造该 command。
- 用 `ParamSpec`/`TypeVar` 或明确 Protocol 表达 node/reducer；不得用 `callable()`、`inspect.signature()` 作为执行期能力发现。
- `Annotated` metadata 只接受封闭 marker；JSON Schema 是 definition 投影，不是 authoritative parameter state。
- 不预设一个 `StateReducer[ValueT]` 能解决异构 state；先证明每个 field identity 如何保存其 `ValueT`。
- RunGraph expression evaluator 的动态结果在写入 channel/output 前按 definition 中声明的 canonical field contract 严格验证；evaluation error、unsupported value、non-finite value 和 type mismatch 返回 typed node failure，不进入 state。
- Workflow node/router 只取得 definition 声明的 typed input projection与允许写入的 command/result surface；不能通过完整 GraphState 读取未声明字段或原地 mutation。Engine 仍拥有 canonical whole-state merge。

完成条件：错误 node I/O、reducer value、parameter binding 静态失败；动态 expression result mismatch 在 commit 前失败；恢复只依赖 definition identity 和 durable state；Workflow domain/durability tests 通过。

### W5A：Tool execution core（D9）

依赖：W0、W2A；若 Hook 不作用于 tool arguments，可用消费者证据解除 W2A 依赖。

- 复用 `ExecutableToolBinding`、canonical invocation identity、permission/effect/audit contracts。
- `ToolExecution` 只持 immutable binding snapshot 与 typed invocation，不持 `tool: Any` 或 live catalog entry。
- 参数按 definition decoder 在入口验证；同一 canonical arguments digest 贯穿 authorization、effect intent、execute、receipt、audit、settlement。
- signature inspection 只允许在 Product function-tool adapter 构建期；Runtime 每次执行路径不得 probing。
- Tool/policy extension 只获得最小 invocation、permission facts 和 monotonic-narrowing decision surface；不注入完整 RunContext、Role、tool catalog 或 EngineServices。

完成条件：D9 清空；ToolExecutor 仍为唯一 chokepoint；permission/effect/in-doubt/retry tests 通过且无旁路。

### W5B：Background capability（D8）

依赖：W5A。

- 用 definition-owned typed execution disposition 替代 `_bg_tool` marker；Product catalog 显式选择发布。
- decorator 用 `ParamSpec` 保留 callable shape，但不成为第二 registry 或执行入口。
- foreground/background 共享同一 ToolExecutor permission/effect 链；BackgroundTaskPool ownership、TaskId/AttemptId 与 lifecycle 不因本包改变。

完成条件：D8 清空；无 marker discovery；同 Pool ownership、foreground/background permission/effect、type fixtures 通过。

### W6A：FileOps Protocol 逃逸（D10）

依赖：W0。

- 删除对内部已装配 `ExternalArtifactRootSource` 的 `callable(getattr())`；由类型化 composition 保证来源。
- 若存在外部 plugin 动态输入，验证必须位于 Product adapter 并投影为 Runtime Port，不能留在 FileOps owner 内。

完成条件：D10 清空；错误来源在 composition/adaptor fail closed；artifact reachability/GC tests 通过。

### W6B：已确认扩展债务整改与最终 baseline 收敛（D11-D23、D25-D29、D31-D32、D34-D38）

依赖：W1A-W6A。

- 将上表每个 confirmed symbol 写入精确 baseline，按 owner 拆成可独立验收的垂直切片；不得用类别级记录代替 symbol identity。
- confirmed debt 通过 canonical DTO、窄 Protocol、tagged union、typed binding/token 或显式 catalog 删除；合法边界只登记 D24/D30/D33 中实际核实过的精确 symbol。
- 每个 `cast` 必须由同函数先行验证建立不变量；`object` 仅用于未验证输入或完全不读取的 opaque token。
- 不允许以 alias、wrapper、ignore、新 facade、宽 Mapping 或新增登记项换名保留债务。

完成条件：全部 `CONFIRMED_DEBT` symbol baseline 为空；D24/D30/D33 只保留附带 owner、消费者数据流、边界理由和验证证据的精确 `VALID_BY_DESIGN` symbol；不存在类别级 waiver。合法动态登记只剩经批准的窄边界，且所有 review_after 未过期。

## 7. 每个工作包的强制开工记录

开始改代码前，在 PR/实施记录中填写：

1. `git status --short --branch` 与目标文件 diff；
2. canonical owner、核心不变量、durability/lifecycle；
3. `rg` 搜到的现有 DTO、Port、codec、binding、registry 及复用/拒绝理由；
4. 所有生产 consumers、composition、public exports；
5. 稳定面、真实变化轴、权限与失败语义；
6. durable/wire 格式是否变化及 migration 决定；
7. contract、implementation、consumer、retirement、gate 的文件清单；
8. 正常、malformed、generic mismatch、ownership、import/layer 验证命令；
9. 最终 diff、未运行测试与剩余 baseline。

状态仅允许：

```text
PROPOSED -> CONFIRMED -> IN_PROGRESS -> DONE
                |             |
                +-> BLOCKED <-+
PROPOSED/CONFIRMED -> REJECTED_AS_VALID_BOUNDARY
```

`DONE` 不允许携带无期限 waiver、旧签名 alias、双 DTO/decoder、`Any` fallback、待清理 marker 或未决定的 durable migration。

## 8. 可执行顺序

严格顺序如下；只有标注可解除依赖的子切片在留下证据后才可提前：

```text
W0 可信门禁与债务 baseline
 -> W1A 模型 failover 泛型
 -> W1B Gateway 实现签名
 -> W2A Hook contracts
 -> W2B Event families
 -> W2C Conversation 生命周期
 -> W2D Model profile transformer
 -> W3 Kernel command/message
 -> W4A Workflow 生命周期/definition
 -> W4B Workflow callable/reducer/parameter
 -> W5A Tool execution core
 -> W5B Background capability
 -> W6A FileOps Protocol 逃逸
 -> W6B 最终 baseline
 -> 全仓独立验收
```

这是默认施工队列，不表示所有包在逻辑上互相依赖。选择顺序的目的，是先稳定扫描与模型/Contract 边界，再处理 Kernel、Workflow 和 Tool 的高风险状态链，最后统一清账；每次只允许一个工作包处于 `IN_PROGRESS`。

## 9. 分层验证与最终验收

每个切片至少包含：

- 一个 pass fixture，证明真实泛型关系端到端推导；
- 一个 mismatch fixture，证明错误实参被预期 Pyright rule 拒绝；
- 一个 runtime negative test，证明 malformed shape fail closed；
- 一个 ownership/composition test，证明没有第二 DTO、registry、callback 或执行路径；
- 相关 import/layer gate；
- 精确 domain tests。

### 9.1 验证矩阵

| 层次 | 每个切片 | 阶段结束 | 最终关闭 |
|---|---|---|---|
| AST governance | 目标 category fixtures、new/stale baseline | 全部已启用规则与合法边界登记 | 全生产 roots，baseline 必须为空 |
| Static typing | 目标 pass/fail fixture与触及的 production symbols | 该阶段 governed-symbol 配置 | 全生产目录；预期 fail fixtures 由 verifier 单独结算 |
| Contract/codec | mismatch、unknown/extra/missing/wrong primitive | 目标 domain codec suite | 所有受影响 durable/wire schema |
| Runtime behavior | 正常、拒绝、异常、取消、cleanup | owner domain tests | Product construct/activate/run/resume/shutdown smoke |
| Architecture | owner、consumer、composition、layer gate | `ztest/architecture` | `ztest/architecture` 加全仓 import/collection gate |
| Retirement | `rg` consumer/export/alias 检查 | 阶段旧入口 inventory 归零 | 全仓旧 DTO/callback/mapping/ignore baseline 归零 |

Pyright 结果必须把生产错误、预期 fail fixture、verifier 自身错误分开结算；一个非零总数或“错误数下降”都不是证据。pytest 必须记录命令、收集数量、通过/失败/跳过、首个失败和未运行范围。资源原因未运行的 suite 明确写 `NOT_RUN`，不得继承历史 PASS。文档、source grep 和生成 JSON 只能作为辅助 evidence，不能单独签署 `DONE`。

### 9.2 切片签收证据

每个工作包的最终记录至少包含：

```text
work_package
baseline_identities_removed[]
valid_boundaries_added_or_changed[]
canonical_types_reused_or_added[]
consumers_migrated[]
old_surfaces_removed[]
durable_or_wire_decision
commands[]: command + exit_code + result_summary
not_run[]
remaining_failures[]: owner + reason + unrelated/related disposition
diff_or_commit_identity
reviewer_disposition
```

相关失败未解决时不得签署 `DONE`。无关预存失败可以记录，但必须由目标范围检查证明本切片没有扩大它；不得修改无关代码换取绿灯。`reviewer_disposition` 必须由独立复核产生，实施者的自述不能替代。

最终关闭必须全部满足：

1. D0-D11 均为 `DONE`，D12-D38 无 `PENDING_VERIFICATION`；所有 confirmed debt 已关闭，所有 valid-by-design 均有逐 symbol 消费者证据；
2. `typecheck/dynamic-type-debt.json` 为空，并由 stale/new-debt 门禁证明；
3. Contracts public DTO/Port 无裸 dict、裸 Callable、无界 Any、未类型化 kwargs；
4. 已知 shape 使用 authoritative DTO/tagged union，批准开放 JSON 使用 canonical JSON 与严格 decoder；
5. 泛型实参从 definition 到 runtime/callback/result 不中断；
6. Protocol 实现不扩大参数面；内部 capability 无反射、私有 marker 或执行期 signature probing；
7. durable/wire decoder 对未知 version/tag、额外/缺失字段和错误 primitive fail closed；
8. 合法动态登记逐项具备 owner、validation、边界测试和 review date；
9. 仓内消费者全部迁移，旧 DTO/callback/mapping API/re-export/compat path 删除；
10. `ztest/architecture`、type-contract verifier、各工作包 Pyright 配置、相关 domain tests 和最终全仓验证通过；

## 10. 开工命令契约

W0 开工前先重新运行并记录，而不是复用本文结果：

```text
git status --short --branch
python -B -m ztest.architecture.static_governance governed-boundary
python -B -m ztest.architecture.static_governance dynamic-discovery
python -B typecheck/verify_type_contract_cases.py
pyright --outputjson
```

命令失败必须区分：目标切片缺陷、既有用户改动、预期负例或环境问题。不得为了绿灯修改无关文件、删除用户改动或放宽门禁。

## 11. 本次审核验证记录

本次审核只修改本文，未修改生产源码或测试。2026-08-04 实际执行/核实：

```text
python -B typecheck/verify_type_contract_cases.py
PASS（exit 0，无输出）

typecheck/dynamic-boundaries.json
存在 4 条文件级声明；全仓无生产或测试代码读取该文件

ztest/architecture/test_dynamic_boundary_governance.py
当前复制 production path、qualified-name 与部分 AST rule；尚未委托 static_governance.py

contracts/model/profile.py::json_schema_transformer
全生产无读取点；命中只存在于定义/注释和测试
```

同时核实：`pyrightconfig.json` 仍为 basic；verifier 自身的动态 JSON 类型在全仓 Pyright 中不闭合；现有 telemetry 动态登记描述的 TypeGuard 与当前 cast 实现不一致；presentation 动态登记描述的 reflective lookup 与当前 `BaseConsumer._handler_for` 默认 closed-table seam 不一致。

以上只读结果用于纠正文档事实，不能作为任何 D/W 工作包的完成证据。实施阶段仍需按第 7 节逐包重建 consumer、composition、durability 和 diff 清单。
