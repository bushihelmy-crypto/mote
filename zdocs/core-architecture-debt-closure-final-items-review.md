# 核心架构债务末尾关闭项审核

审核对象：`core-architecture-debt-closure-implementation.md` 末尾“本次 IN_PROGRESS → DONE 记录”中的 17 个工作包。

审核日期：2026-08-02

审核方式：只读检查当前工作树、实施总账、工作包原始关闭条件及相关生产源码。本次未修改项目代码。

## 1. 结论

末尾列出的 17 个工作包目前不能整体标记为 `DONE`。

- `R2.8`、`R2.10`、`R2.44`、`R2.45` 存在当前生产源码直接反证，应退回 `IN_PROGRESS`。
- 其余 13 项在本轮静态抽查中未确认新的直接实现反证，但缺少实施手册要求的完整、可复现签收证据，不能据此确认真正关闭。
- 依赖上述未关闭工作包的下游项需要重新计算硬前置闭包；硬前置未达到 `DONE/REJECTED` 的工作包不能维持 `DONE`。

因此，文档末尾的整体 `IN_PROGRESS → DONE` 记录当前不成立。

## 2. 直接反证

### 2.1 R2.10：Agent wiring 与 Runtime client context 未真正收窄

工作包要求：

- Product 将根配置投影为最小、Contracts-owned、经过批准的 activation spec；
- Runtime/Agent 不得取得 raw Hook/MCP config、原始路径 root 或无关 capability；
- `AgentWiring/EngineServices` 不得成为跨 bounded context 的公共 service locator；
- 每个组件只能接收自己的窄 immutable input 或 Port。

当前 `runtime/agent/wiring.py::AgentActivationInputs` 仍公开携带：

- `HookConfig`；
- `tuple[MCPServerConfig, ...]`；
- `primary_config_path`；
- `config_secret_predicate`；
- `watched_config_files`；
- `user_config_root`；
- `source_digest + approved` 布尔组合。

同一 wiring 还继续传播 session、secret、browser、sandbox CA、OAuth root，以及任意 routing builder factory。

这并不是最小 approved activation projection，而是把原始配置、路径和多类 capability 重新聚合进 Runtime wiring。仅增加 `approved: bool` 和 `source_digest` 无法证明每项配置与路径均绑定 canonical source、ownership、content digest、trust decision 和 approval identity。

直接证据：

- `runtime/agent/wiring.py:33-86`

判定：`IN_PROGRESS`。

### 2.2 R2.8：易变 turn-context 仍进入 durable conversation history

工作包明确要求 cwd、time、git、token pressure、BackgroundTask/LSP 通知等易变内容只能进入 user prompt system-reminder，不得成为 authoritative durable history。

当前 Protocol 将 `save_to_context=True` 定义为：通过 `collect_to_context()` 写入 history，并跨 turn 和 compaction 保留：

- `contracts/ports/conversation/turn_context.py:75-85`

与此同时，Git source 明确声明：

```python
save_to_context = True
```

直接证据：

- `runtime/context/turn/sources/git.py:53-59`

Git 工作树状态属于工作包正文列出的易变内容，却仍被写入 durable conversation history，与关闭条件直接矛盾。

判定：`IN_PROGRESS`。

### 2.3 R2.45：Prompt、cache、compaction 与 generation identity 未闭合

R2.45 再次明确要求：

- cwd/time/git 等运行态内容只进入 request-only user prompt system-reminder；
- 不得进入 static prefix 或 durable conversation history；
- compaction 后应从 canonical fact/reference 重建，不应把易变 prompt 文本当作 durable truth。

当前 Git source 的 `save_to_context=True` 与上述要求直接冲突，且 Protocol 文档明确承诺该内容跨 compaction 保留。因此，这不是单纯注释或命名问题，而是实际 prompt/history lifecycle 不符合要求。

直接证据：

- `contracts/ports/conversation/turn_context.py:75-85`
- `runtime/context/turn/sources/git.py:53-59`

判定：`IN_PROGRESS`。

### 2.4 R2.44：Telemetry 仍通过运行时 TypeGuard 猜型

工作包要求：

- `EventT` 从 typed emitter 贯穿到 typed handler；
- 异构存储所需的类型擦除只能封装在 Runtime owner 私有层；
- binding 关系应在构造时验证；
- 不得通过运行时 `TypeGuard` 猜测 event 类型。

当前实现仍定义：

```python
class _EventNarrower(Protocol[EventT_co]):
    def __call__(self, event: object) -> TypeGuard[EventT_co]: ...
```

随后 `_NarrowingAsyncHandler` 和 `_NarrowingSyncHandler` 在每次处理 event 时调用 narrower，按运行时判断决定是否投递。这正是工作包要求删除的 `object + TypeGuard` runtime narrowing seam。

直接证据：

- `runtime/events/telemetry.py:24-27`
- `runtime/events/telemetry.py:44-98`

判定：`IN_PROGRESS`。

## 3. 其余工作包的审核状态

| 工作包 | 本轮结论 | 说明 |
| --- | --- | --- |
| R0.3 | 尚不能确认关闭 | 未发现新的直接静态反证，但需要可复现的媒体授权、reservation、download failure、partial settlement 和 crash/in-doubt 测试证据。 |
| R1.13 | 尚不能确认关闭 | durable delivery 涉及重启、claim/ack、stale incarnation、poison payload 和广播逐目标结算，不能仅由源码扫描签收。 |
| R2.7 | 尚不能确认关闭 | 需要精确 Pyright 命令及 Kernel→Runtime request 运行时 decoder/negative fixture 证据。 |
| R2.8 | 未关闭 | Git 易变内容仍进入 durable history。 |
| R2.10 | 未关闭 | Runtime wiring 仍传播 raw config、路径 root 和宽 capability 集合。 |
| R2.11 | 尚不能确认关闭 | 需要证明 CLI、hosting、ACP/AG-UI 只有一个 typed hosting owner，且生产路径不存在 `Any`/反射 Role 能力判断。 |
| R2.12 | 尚不能确认关闭 | 当前已修复已知 `ContextManagerConfig` 反证，但仍需完整配置 consumer matrix、唯一 base gate 和迁移前后语义证据。 |
| R2.14 | 尚不能确认关闭 | 当前 `GraphAssemblyInputs` 已类型化，但仍需对 graph nodes、ExecutionEngine 和替代 builder 做完整 Pyright 验证。 |
| R2.15 | 尚不能确认关闭 | 泛型 spawn 链、dynamic output schema、policy extension 和 capability 隔离需要精确 Pyright 与运行时 negative fixtures。 |
| R2.17 | 尚不能确认关闭 | nickname/path/Agent ID 的并发、崩溃、回收、retention 和 ABA 关闭条件需要确定性 fault injection。 |
| R2.18 | 尚不能确认关闭 | 需要 ChildAgentHandle 泛型全链 Pyright 和 teardown/rollback/release 生命周期测试。 |
| R2.28 | 尚不能确认关闭 | 跨 supervisor 重启 lineage、预算恢复、lease fence 与 tombstone 需要真实 reopen/crash 证据，并依赖 R2.15 等前置真正关闭。 |
| R2.38 | 尚不能确认关闭 | 需要证明有限 operation 在 wire 前拒绝、failover subset 过滤及 transport registry 双向一致。 |
| R2.39 | 尚不能确认关闭 | 需要 native/XML、schema、多模态和 operation 异构组的排序无关与 endpoint pinning 行为测试。 |
| R2.44 | 未关闭 | 仍存在 `object + TypeGuard` runtime narrowing。 |
| R2.45 | 未关闭 | Git 动态上下文仍持久化进入 history。 |
| R2.53 | 尚不能确认关闭 | subtree cancellation 的 durable epoch、冻结 snapshot、逐 Agent settlement、owner loss/timeout 和资源结算需要 fault injection；同时依赖 R2.15/R2.28 等闭包。 |

“尚不能确认关闭”不等同于已经证明实现错误，而是当前证据不足以满足本文自身的 `DONE` 定义。

## 4. 台账证据不满足文档自身要求

实施手册第 3.2 节要求每个 `DONE` 工作包记录：

- 实际测试命令；
- 通过和失败数量；
- fault-injection 场景；
- Pyright 结果；
- 预存失败和未运行范围；
- commit、变更集标识或工作区 diff 摘要。

当前 17 项的台账主要记录“若干断言通过”和概括性的“归零”结论，但没有提供可直接复现的完整命令，也没有对应 commit 或明确工作区 diff 标识。部分行使用“原有证据保持有效”“定向门禁通过”等描述，不能证明这些结果对应当前工作树。

当前仓库还是大规模 dirty worktree，且本实施文档本身为未跟踪文件。因此无法把台账中的测试数量可靠绑定到稳定代码快照。

按手册自己的状态规则，这些证据不足以支持 `DONE`。

## 5. 测试环境限制

仓库说明要求开发和测试统一使用 `mgx-codex-cli` conda 环境；当前环境列表中不存在该环境。本轮没有擅自选择其他环境执行测试。

因此，本报告没有把未运行的行为测试、并发测试、崩溃恢复、跨进程测试、真实 provider 测试或 Pyright 结果推定为通过。

## 6. 建议修正

1. 将 `R2.8`、`R2.10`、`R2.44`、`R2.45` 退回 `IN_PROGRESS`。
2. 重新计算硬前置闭包；依赖上述工作包的下游项同步退回，直到前置恢复 `DONE/REJECTED`。
3. 对其余 13 项保留 `IN_PROGRESS`，逐项补充实际命令、失败数量、未运行范围、Pyright、fault injection 和变更集标识。
4. 为上述四个直接反证增加能够失败的架构 gate：
   - 禁止 git/cwd/time source 使用 durable-history disposition；
   - 禁止 Agent wiring 暴露 raw config/path roots 和宽 factory mapping；
   - 禁止 telemetry typed binding 使用 runtime `TypeGuard` 恢复 EventT；
   - 验证 compaction/rebuild 后动态 source 只重新投影到 request-only reminder。
5. 在指定测试环境明确后，重新执行对应直接测试、消费者测试、`ztest/architecture`、Pyright 及并发/崩溃 fault injection。

## 7. 最终判定

末尾 17 个需求序号没有达到整体关闭条件。

当前可以确定至少 4 项仍存在生产实现反证；其余 13 项缺少足以复现和绑定当前工作树的完整签收证据。文档末尾的 `IN_PROGRESS → DONE` 清单应撤销或重新标记，待实现反证修复、依赖闭包恢复且验证证据补齐后再签收。
