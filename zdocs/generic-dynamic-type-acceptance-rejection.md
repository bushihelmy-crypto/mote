# 泛型与动态类型债务逐项验收退回单

日期：2026-08-04  
范围：`contracts/`、`kernel/`、`runtime/`、`orchestration/`、`product/`

## 独立复验结论（2026-08-04）

当前结论为 **ACCEPTED / CLOSED**。下文原始退回表保留为历史证据；本节是当前
authoritative verdict。

### 继续复验结论

上一版提前声明五项全部关闭；本轮直接检查源码、evidence 关联和实际测试后予以纠正：

- D7、D15、D35 达到闭合条件：durable definition 与 process-local executable 已分离；
  `Stage[T]`/deferred result 保留泛型；Product inspection 只使用 immutable views。
- D3 达到闭合条件：四个 ledger 登记 decoder 均有 missing、extra、wrong-primitive 负例；
  D3 validation evidence 已收敛到直接覆盖全部登记 symbol 的测试。
- D11 达到闭合条件：门禁逐项验证 consumer 对 owner/symbol 的引用，以及 validation test
  对 owner/symbol/consumer identity 的关联；不相关 evidence 有显式拒绝负例。D21 的配置型
  关系使用专门规则验证 consumer 未被 Pyright 排除且 validation 确实执行 Pyright。

- D6 已通过复验：`CommandChannel.history_projection` 只接受 canonical `Message`，使用
  `encode_message` 形成 fingerprint；`list[Any]`、`hasattr(model_dump)` 与
  `json.dumps(default=str)` 已删除。
- D3 生产改动已通过静态复验：`contracts/events/` 公开 event payload/decoder 使用 canonical `JsonValue`
  和严格字段 decoder；目标目录不再命中 `dict[str, Any]`、`Dict[str, Any]` 或
  `Mapping[str, Any]`。
- D7/D15 已通过复验：`Stage[T]`、deferred executor/poll/result 与 Workflow definition/state/result
  主链保留类型参数；`definition.py`、`engine.py` 和 `notify.py` 的目标宽类型链已退出，重复的
  `GraphRunState.ensure` decorator 已删除。
- D11 已通过复验：schema v2 验证路径、AST identity、consumer dependency 和 validation
  evidence 关联，并拒绝不相关 evidence。
- D35 已通过复验：deferred metadata 不再泄漏 live graph 或 mutable state；Product inspection 使用
  immutable `WorkflowNodeView`、`WorkflowGraphView`、`WorkflowRunView` 投影。
- D20 实现签名已经修正为 `Callable[[], float] | None`；原 evidence 错指不相关的
  `test_bus_bridge.py`，本轮已纠正为直接覆盖 `CircuitBreaker` 的 `test_breaker.py`，可关闭。

### D0-D38 当前裁决

| ID | 当前裁决 | 说明 |
|---|---|---|
| D0 | CLOSE | provider/request/result/provider-key 泛型 attempt 链已在 owner 中保持。 |
| D1 | CLOSE | 三个 Gateway 不再以宽 `**kwargs` 重写 Port。 |
| D2 | CLOSE | Hook invocation 已拆为 typed DTO，JSON 字段 admission 后冻结。 |
| D3 | CLOSE | 四个登记 decoder 均有 missing/extra/wrong-primitive 负例并通过。 |
| D4 | CLOSE | Message domain、provider projection 与 session codec 已分离。 |
| D5 | CLOSE | `json_schema_transformer` 已删除且无生产消费者。 |
| D6 | CLOSE | Kernel history projection 已改为 canonical `Sequence[Message]`。 |
| D7 | CLOSE | Workflow definition/state/deferred 主链已参数化并分离 durable executable contract。 |
| D8 | CLOSE | background marker discovery 旧路径已删除。 |
| D9 | CLOSE | ToolExecutor 使用 immutable executable binding，旧 live-tool probing 已退出。 |
| D10 | CLOSE | 原 `callable(getattr(...))` capability 验证路径已退出。 |
| D11 | CLOSE | evidence 门禁验证 consumer/test 关联，并有不相关 evidence 拒绝负例。 |
| D12 | CLOSE | `MessageQueue` 目标 public surface 已显式注解。 |
| D13 | CLOSE | 目标 capabilities 已使用精确 Callable/Protocol。 |
| D14 | CLOSE | 目标 durable/public wire 已改用显式字段 encoder；未发现原自动 encoder 路径。 |
| D15 | CLOSE | `Stage[T].execute() -> T`，deferred/engine/definition 泛型关系端到端保留。 |
| D16 | CLOSE | `ToolArguments` deep-freeze 后贯穿目标 permission/effect/execution 链。 |
| D17 | CLOSE | 已列举的有限状态已改为 enum/tagged owner。 |
| D18 | CLOSE | telemetry erasure 封装于 private nominal-token binding。 |
| D19 | CLOSE | session JSON 解码集中在 strict codec，未见旧 primitive coercion。 |
| D20 | CLOSE | 实现签名已修正，evidence 已纠正为直接覆盖 `CircuitBreaker` 的测试。 |
| D21 | CLOSE | stale/broad Pyright ignore 已退出；全生产 Pyright 可执行通过。 |
| D22 | CLOSE | resume update 经过 strict state validation。 |
| D23 | CLOSE | 目标 pickle/`__dict__.update`/`SerializeAsAny` 路径已退出。 |
| D25 | CLOSE | 目标 SQLite/session decoder 已有 strict primitive/shape 校验。 |
| D26 | CLOSE | BackgroundTask result 已使用不可混配的 foreground/background/hybrid variants。 |
| D27 | CLOSE | 目标 pool/Port/result 路径使用 `TaskId`/`AttemptId`。 |
| D28 | CLOSE | tool schema admission 使用递归 freeze，DTO 不保存调用方 mutable dict。 |
| D29 | CLOSE | 与 D8 同切片删除 marker discovery。 |
| D31 | CLOSE | RunGraph 目标 state/channel assignment 已有 strict commit validation。 |
| D32 | CLOSE | generated RPC 隔离收窄，手写 wrapper 进入 Pyright。 |
| D34 | CLOSE | 目标 Workflow/Tool definition identity 不再依赖 qualname/location。 |
| D35 | CLOSE | Workflow presentation 只暴露 immutable inspection DTO，不泄漏 live execution object。 |
| D36 | CLOSE | runtime awaitable probing 只剩两个已登记外部 SDK adapter。 |
| D37 | CLOSE | 目标 aiohttp AppKey 已绑定具体 authoritative type。 |
| D38 | CLOSE | ComponentKey 使用对象 identity；同名 token 不能命中 slot。 |

本轮总计：36 项关闭；此前剩余的 **D3、D11** 已复验关闭。

复验命令与结果：

| 验证 | 结果 |
|---|---|
| `pyright contracts kernel runtime orchestration product engine.py` | 0 errors, 0 warnings |
| `python -B typecheck/verify_type_contract_cases.py` | PASS |
| `confirmed-dynamic-debt-symbols` / `dynamic-boundary-registry` | invariant closed |
| Workflow + Native Channel 聚焦套件 | 100% PASS |
| D3 四个登记 decoder malformed 负例 | 4 passed（每项覆盖 missing/extra/wrong primitive） |
| D11 evidence linkage 正反例 + static governance | 2 passed；invariant closed |
| ToolExecutor/Tool snapshot/Binding/BackgroundTask 聚焦套件 | 100% PASS |
| Gateway/SQLite identity/ComponentKey/Lifecycle 聚焦套件 | 100% PASS |
| Model/Workflow/Tool/Process/Connection 架构聚焦套件 | 100% PASS |

静态门禁和 Pyright 通过不覆盖上述测试与 evidence 关联缺口，不能用于关闭 D3/D11。

## 总结

本次逐项验收不接受原账台的 `CLOSED / VERIFIED` 声明。架构门禁、Pyright 和架构测试均可通过，但账台的 authoritative baseline 只有空 `records`，门禁主要是有限字符串黑名单，不能证明每个 D 项的真实消费者、泛型关系、生命周期和唯一执行入口已闭合。

以下结论是本轮验收结论：`REOPEN` 表示退回重新修改；`EVIDENCE_INSUFFICIENT` 表示不得维持 VERIFIED，必须补证后重新验收。

## 逐项结论

| ID | 结论 | 验收证据 / 退回要求 |
|---|---|---|
| D0 | EVIDENCE_INSUFFICIENT | 未建立 provider/request/result 的逐 symbol 证据链；需补齐 attempt state、selector、observer、result 的生产消费者和负例测试。 |
| D1 | EVIDENCE_INSUFFICIENT | Pyright 通过不能证明所有 gateway 实现与 Port 参数完全一致；需逐实现签名检查和运行时调用测试。 |
| D2 | EVIDENCE_INSUFFICIENT | 未逐一核对 invocation DTO、outcome、permission 收窄及 wire decoder；需补 exact-shape 负例。 |
| D3 | EVIDENCE_INSUFFICIENT | 当前门禁仅覆盖少数 event 模块，未证明全部公开事件与 codec 的字段闭合；需按 domain 建立完整清单。 |
| D4 | EVIDENCE_INSUFFICIENT | 未证明 Message、provider projection、durable codec 的所有消费者均已迁移；需搜索并删除旧适配入口。 |
| D5 | EVIDENCE_INSUFFICIENT | 字段搜索显示 transformer 名称已消失，但缺少历史消费者迁移和公开导出核验；补 consumer/export gate。 |
| D6 | **REOPEN** | `kernel/commands/channel.py:129` 仍为 `history_projection(messages: list[Any])`，并使用 `hasattr(..., "model_dump")` 与 `default=str`。与 canonical message union 要求直接冲突。 |
| D7 | **REOPEN** | `orchestration/workflows/types.py`、`deferred.py`、`control.py` 仍有 `Any`、裸 `set/list`、`Callable[[], Coroutine]` 和动态 graph/state fallback。 |
| D8 | EVIDENCE_INSUFFICIENT | 未证明 decorator、catalog、执行入口只有一套；需提供 marker 删除的全仓搜索和 catalog composition 测试。 |
| D9 | EVIDENCE_INSUFFICIENT | 未完成 ToolExecutor 唯一 chokepoint 的旁路搜索；需核对 live tool、signature probing、permission/effect/audit 全链。 |
| D10 | EVIDENCE_INSUFFICIENT | 未证明所有 artifact root source 均由 typed composition 保证；需补内部 source 的构造与错误路径测试。 |
| D11 | **REOPEN** | `dynamic-type-debt.json` 的 `records` 为空，门禁只校验固定黑名单；没有逐 symbol baseline、owner、consumer、验证测试和 stale identity 检查。 |
| D12 | EVIDENCE_INSUFFICIENT | Queue 当前方法有注解，但“全部 governed public/private surface”没有完整枚举；需生成 public-surface 清单并验收。 |
| D13 | EVIDENCE_INSUFFICIENT | 未逐 capability 核实参数/返回关系和异步语义；需移除宽 alias 并增加 mismatch fixtures。 |
| D14 | EVIDENCE_INSUFFICIENT | 未证明所有 event/session/public wire 均使用显式 encoder；需全生产自动序列化扫描及 extra/missing 负例。 |
| D15 | **REOPEN** | Workflow 类型链仍存在 `Any`、裸集合和未参数化 `Coroutine`；不能声称 definition→runtime→result 泛型关系贯穿。 |
| D16 | EVIDENCE_INSUFFICIENT | `ToolArguments` 已出现，但未核实 policy/effect digest 的所有边界均拒绝宽 mapping；需逐消费者搜索。 |
| D17 | EVIDENCE_INSUFFICIENT | 未建立所有闭集字符串字段的 domain inventory；需 enum/tagged union 清单和 unknown-value 测试。 |
| D18 | EVIDENCE_INSUFFICIENT | registry 有 telemetry 记录，但未独立证明 nominal token 不可伪造及 subclass 负例覆盖生产 dispatch。 |
| D19 | EVIDENCE_INSUFFICIENT | 未逐 codec 核验 exact fields、version、primitive 和未知 tag；需运行 malformed durable fixture 集。 |
| D20 | EVIDENCE_INSUFFICIENT | 未独立检查 `CircuitBreaker` override、wrapper 和 default sentinel 的完整签名链；需补 pyright 与运行时测试。 |
| D21 | EVIDENCE_INSUFFICIENT | Pyright 通过不等于 stale/broad ignore 全部退出；需解析配置并验证每个 generated/manual 路径。 |
| D22 | EVIDENCE_INSUFFICIENT | 未直接测试 `resume_plan` 的错误 primitive、variant 和跨字段不变量；需补 strict transition 负例。 |
| D23 | EVIDENCE_INSUFFICIENT | 未证明所有 error/message durable consumers 已退出 pickle、`__dict__.update`、`SerializeAsAny`；需全仓扫描。 |
| D25 | EVIDENCE_INSUFFICIENT | SQLite/session decoder 需逐字段验证 extra/missing/wrong primitive；当前门禁覆盖不足。 |
| D26 | EVIDENCE_INSUFFICIENT | 未对每个 foreground/background/hybrid 非法字段组合做构造失败测试。 |
| D27 | EVIDENCE_INSUFFICIENT | 未完成 TaskId/AttemptId 从 Port 到 notification 的全链路 identity 审计。 |
| D28 | EVIDENCE_INSUFFICIENT | 未证明 deep-freeze 是递归且 query projection 不共享可变引用；需 mutation-after-admission 测试。 |
| D29 | EVIDENCE_INSUFFICIENT | 与 D8 相同，需全仓确认 setattr/getattr marker 和第二 registry 均不存在。 |
| D31 | EVIDENCE_INSUFFICIENT | 未验证每次 RunGraph state commit 的 schema、assignment 和 node failure 原子性；需错误结果不写 state 的测试。 |
| D32 | EVIDENCE_INSUFFICIENT | 未核验 generated 文件边界及手写 RPC wrapper 的每个 DTO 转换；需精确 manifest。 |
| D34 | EVIDENCE_INSUFFICIENT | 未证明所有 durable identity 均为显式稳定 identity；需扫描 digest、compiler 和 migration。 |
| D35 | **REOPEN** | Workflow control/deferred/state 仍把完整动态对象作为边界（见 D7 证据），未形成最小 immutable DTO/Protocol。 |
| D36 | EVIDENCE_INSUFFICIENT | 未列出全部 sync/async lifecycle callback，也未证明 adapter 外没有 awaitable probing；需 registry 与负例。 |
| D37 | EVIDENCE_INSUFFICIENT | 未逐一核对 11 个 AppKey 的 put/get 类型一致性及 activation 失败语义。 |
| D38 | EVIDENCE_INSUFFICIENT | 未证明 ComponentKey token 的 nominal identity 不可由字符串/cast 伪造；需错误 token、同名异型和注册冲突测试。 |

## 必须重新修改的项目

立即重开：**D6、D7、D11、D15、D35**。  
其余项目不得继续标记 `VERIFIED`，在补齐逐项证据前统一保持 `EVIDENCE_INSUFFICIENT`，并进入重新修改/重新验收队列。

重新提交必须包含：逐 symbol baseline、canonical owner、全部生产消费者、composition/lifecycle/wire/durable 证据、旧入口删除证明、静态 mismatch fixture、malformed runtime negative tests，以及实际执行命令和结果。仅修改账台、清空 `records`、增加字符串黑名单或通过目录 ignore 不构成修复。
