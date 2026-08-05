# ReactGraph 静默终止与原子 Session 事实实施文档

## 1. 实施目标与依据

本文是 [`react-graph-quiescence-and-atomic-session-requirements.md`](./react-graph-quiescence-and-atomic-session-requirements.md) 的唯一实施说明。目标是在一个闭合迁移切片内完成：

- 最终候选先等待静默，静默后才验证并原子提交；
- 用户消息和后台任务通知统一通过 Agent 私有 `message_buffer` 唤醒；
- 删除 `WAIT_BACKGROUND` 与输出 `accepted -> commit_started -> committed` 旧链；
- OBSERVE、THINK、ACT 和最终输出使用明确的原子事实批次；
- 恢复只读取一个 canonical 终态事实；
- Graph 与发布生命周期彻底分离；
- 所有仓内生产消费者、测试、codec、projection 和门禁在同一切片迁移，不保留 alias、fallback 或双读。

当前工作区已有两项相关修改，实施时必须保留并纳入最终测试：

- `runtime/agent/role.py::publish_message()` 的空目标与混合目标拆分；
- `kernel/execution/operations/observation.py` 删除 `watch/send_to/<all>` 二次过滤。

必须保持的一条核心控制边是：

```text
THINK
├─ inference completed -> INTERPRET
└─ inference stopped（包括 active=False）-> AWAIT_QUIESCENCE
```

`THINK` 不得直接 `End`，也不得再判断后台任务或进入 `WAIT_BACKGROUND`。

## 2. 当前实现基线

### 2.1 当前 Graph

当前生产拓扑：

```text
RESTORE -> OBSERVE -> BUDGET -> THINK -> INTERPRET
                        ^          |          |
                        |          |          +-> ACT -> OBSERVE
                        |          +-> WAIT_BACKGROUND -> OBSERVE
                        |                     
                        +----------- VALIDATE_OUTPUT -> End
```

当前直接返回 `End` 的位置：

- `RestoreNode`：恢复旧 accepted/committed output；
- `ObserveNode`：首次 buffer 为空；
- `BudgetNode`：预算停止；
- `InferenceNode`：未推理且无后台任务；
- `ValidateOutputNode`：接受并 commit 后结束。

### 2.2 当前消息提交

`runtime/context/history/manager.py::add_batch()` 逐条调用 `add()`；每条消息分别执行：

```text
MessageAppendedEvent
-> SessionFactCommitter.commit_fact()
-> EventFabric.append(stream, one_fact)
-> 内存 messages.append()
```

因此一个 observation batch、一个 model turn projection 或一个 tool result batch 不是原子单元。

### 2.3 当前输出提交

当前输出链跨多个事件和多个 flush：

```text
OutputCandidateReceivedEvent
-> OutputAcceptedEvent
-> final AIMessage MessageEvent
-> checkpoint.record_result()
-> OutputCommitStartedEvent
-> drain
-> OutputCommittedEvent
-> drain
```

`RuntimeExecutionTransaction` 通过进程内 `_staged`、`_terminal` 和 `_operations` 维护第二套状态。这些字段不能证明崩溃原子性。

### 2.4 当前后台等待

`BackgroundTaskPool.wait_any()` 同时等待：

- pool-owned completion future；
- `msg_buffer.wait_for_message()`。

后台 task terminal 又通过 `deliver()` 把通知 push 到相同 buffer。因此 Graph 存在两套唤醒事实。

## 3. 目标生产拓扑

```text
RESTORE
   |
   v
OBSERVE -> BUDGET -> THINK -> INTERPRET
   ^                  |      /       \
   |                  |    ACT   PendingCandidate
   |                  |     |          |
   +------------------|-----+          v
                      +--------> AWAIT_QUIESCENCE
                                  /          \
                        inbox activity       quiescent
                              |                  |
                              v                  v
                           OBSERVE        VALIDATE_OUTPUT
                                             /      \
                                         rejected  committed
                                            |          |
                                            v          v
                                         OBSERVE      End
```

无 candidate 的终止请求：

```text
OBSERVE initial-empty --+
BUDGET stop ------------+-> AWAIT_QUIESCENCE -> End
THINK stopped ----------+
```

其中 `THINK stopped` 是 `THINK -> AWAIT_QUIESCENCE` 的正式图边，不是异常清理或 Graph 外逻辑。

已提交终态的恢复是唯一允许绕过静默门的路径，因为该 run 已不可继续：

```text
RESTORE committed final output -> End
```

## 4. Canonical 类型设计

### 4.1 删除的类型

从 `contracts/output/models.py` 删除：

- `AcceptedOutput`；
- `OutputEvaluationState.ACCEPTED` 及不再使用的 lifecycle enum；
- `OutputDeliveryState.STAGED`、`COMMIT_STARTED`、`PUBLICATION_QUEUED`；
- 如果全仓没有独立消费者，删除整个 `OutputDeliveryState`。

保留 `CommittedOutput[OutputT]`，但其语义收窄为“最终 AIMessage 与类型化输出已经通过一次原子 Session 事务提交”。它不再表示 staged output 的第二阶段结果。

### 4.2 新增 transient candidate

在 `kernel/execution/state.py` 定义 Kernel-owned transient 类型：

```python
@dataclass(frozen=True, slots=True)
class PendingCandidate:
    turn: ModelTurn
    candidate_index: int

    @property
    def candidate(self) -> FinalCandidateAction: ...
```

它与当前 `CandidateSelection` 语义相同，可以直接将 `CandidateSelection` 重命名为 `PendingCandidate`，同一切片迁移全部消费者后删除旧名。不得保留 alias。

`ExecutionState[OutputT]` 调整为：

```python
@dataclass
class ExecutionState(Generic[OutputT]):
    response: Message
    committed_output: CommittedOutput[OutputT] | None = None
    turn: NoModelTurn | ModelTurn | PendingCandidate = field(default_factory=NoModelTurn)
    initial_observe_complete: bool = False
    requested_end: ExecutionResult[OutputT] | None = None
```

约束：

- `PendingCandidate` 只在 `INTERPRET -> AWAIT_QUIESCENCE -> VALIDATE_OUTPUT` 之间存在；
- `AWAIT_QUIESCENCE -> OBSERVE` 前必须把 `turn` 重置为 `NoModelTurn()`；
- `requested_end` 只承载无 final candidate 的预算停止、主动停止或空运行结果；
- `committed_output` 只在原子终态事务成功后设置。

### 4.3 类型化 inference disposition

在 `contracts/execution/models.py` 增加封闭结果：

```python
@dataclass(frozen=True, slots=True)
class InferenceCompleted:
    pass

@dataclass(frozen=True, slots=True)
class InferenceStopped:
    pass

InferenceDisposition: TypeAlias = InferenceCompleted | InferenceStopped
```

`InferenceService.infer()` 返回 `InferenceDisposition`，不再返回 `bool`：

- `active=False` -> `InferenceStopped()`；
- checkpoint reinstate 成功或新模型调用启动成功 -> `InferenceCompleted()`；
- provider/持久化失败继续抛 typed error，不转换成 stopped。

## 5. Inbox activity 单一语义

### 5.1 扩展 Contracts Port

当前 `MessageActivity` 只有 `wait_for_message()`，无法进行无丢失唤醒的静默判定。将它替换为 generation-aware Port：

```python
@dataclass(frozen=True, slots=True)
class MessageActivitySnapshot:
    generation: int
    pending: bool

class MessageActivity(Protocol):
    def activity_snapshot(self) -> MessageActivitySnapshot: ...
    async def wait_for_activity(self, after_generation: int) -> MessageActivitySnapshot: ...
```

删除 `wait_for_message()`；迁移 `BackgroundTaskPool`、`Role.wait_interruptible()` 和其他仓内消费者。

### 5.2 修改 `MessageQueue`

`contracts/conversation/queue.py::MessageQueue` 增加：

```python
_activity_generation: int = PrivateAttr(default=0)
_activity_condition: asyncio.Condition
```

行为：

- 每次成功 `push()`：先追加 item，再递增 generation，再通知 condition；
- `pop/pop_all()` 不回退 generation；
- `activity_snapshot()` 同步返回当前 generation 和 `not empty()`；
- `wait_for_activity(after_generation)`：若当前 generation 已更大或已有 pending，立即返回；否则等待 condition；
- dump/load 只持久化消息，不持久化进程内 generation；load 后若有消息，generation 初始化为 1，否则为 0。

禁止继续使用可被 clear 的单一 `asyncio.Event` 证明“自某次检查以后发生过活动”。

### 5.3 后台 task notification 是唯一完成唤醒

修改 `orchestration/background_tasks/pool.py`：

- 删除供 Graph 使用的 `_next_completion()`/`wait_any()` 路径；
- `BackgroundTaskService` Port 删除 `wait_any()` 与 `BackgroundWakeReason`；
- task terminal producer 继续通过 `deliver()` 先 push `BackgroundTaskNotification`；
- push 失败时 terminal settlement 返回 typed cleanup/delivery failure，不得把 task 当作完全结算；
- pool 自己内部如仍需 completion future 支持 `wait_for_completion()`/release drain，可以保留为 pool-private 实现，但不得作为 Graph 唤醒来源。

## 6. `AWAIT_QUIESCENCE` 实现

### 6.1 新增 NodeId 与节点

在 `kernel/execution/graph/core.py`：

- 删除 `NodeId.WAIT_BACKGROUND`；
- 新增 `NodeId.AWAIT_QUIESCENCE = "await_quiescence"`。

在 `kernel/execution/graph/nodes.py`：

- 删除 `WaitBackgroundNode`；
- 新增 `AwaitQuiescenceNode[OutputT]`，`effect_kind = EffectKind.WAITABLE`；
- `allowed_targets = {NodeId.OBSERVE, NodeId.VALIDATE_OUTPUT}`，无 candidate 的静默路径允许返回 `End`。

构造依赖必须保持窄：

```python
def __init__(
    self,
    inbox_activity: Callable[[], MessageActivity],
    get_bg_pool: Callable[[], BackgroundTaskService | None],
) -> None: ...
```

更优实现是在 `ExecutionContext` 暴露 Contracts-owned `MessageActivity`，Graph assembly 传入具体 Port；节点不得取得完整 Role。

### 6.2 静默算法

伪代码：

```python
async def run(self, state):
    inbox = self._inbox_activity()

    while True:
        before = inbox.activity_snapshot()
        if before.pending:
            state.turn = NoModelTurn()
            state.requested_end = None
            return Transition(NodeId.OBSERVE)

        pool = self._get_bg_pool()
        task_snapshot = pool.pin_snapshot(owner=pool.owner) if pool is not None else None
        if task_snapshot is not None and task_snapshot.pending_count > 0:
            await inbox.wait_for_activity(before.generation)
            state.turn = NoModelTurn()
            state.requested_end = None
            return Transition(NodeId.OBSERVE)

        after = inbox.activity_snapshot()
        if after.pending or after.generation != before.generation:
            continue

        if isinstance(state.turn, PendingCandidate):
            return Transition(NodeId.VALIDATE_OUTPUT)

        return End(state.requested_end)
```

`BackgroundTaskPinSnapshot` 当前字段若不能表达 pending count、in-flight delivery 和 generation，必须在 `contracts/task/lifecycle.py` 的 canonical snapshot 中补齐；不得读取 pool 私有 `_tasks`。

静默成立条件必须覆盖：

- 无运行 task；
- 无 operation/permit/output/notification settlement；
- 无已生成但尚未 push 的 terminal notification；
- inbox generation 在双重检查期间稳定。

### 6.3 candidate 失效

`AWAIT_QUIESCENCE` 发现或等到 inbox activity 时：

- `state.turn = NoModelTurn()`；
- 清空仅服务本次终止请求的 `requested_end`；
- 转 `OBSERVE`。

这样旧 candidate 不会在新用户输入或后台结果之后被验证。

## 7. Graph 节点逐文件改造

### 7.1 `RestoreNode`

文件：`kernel/execution/graph/nodes.py`、`kernel/execution/operations/output.py`

- `OutputOperation.restore()` 只恢复一个已经原子提交的 `FinalOutputCommittedEvent`；
- 有 committed output：返回 `End(ExecutionResult(...))`；
- 无 committed output：转 `OBSERVE`；
- 删除恢复 accepted/staged output 后补 commit 的逻辑；
- `RestoreNode.allowed_targets` 仍只包含 `OBSERVE`。

### 7.2 `ObserveNode`

文件：`kernel/execution/graph/nodes.py`、`kernel/execution/operations/observation.py`

定义类型化 observation result：

```python
@dataclass(frozen=True, slots=True)
class ObservationResult:
    observed_count: int
    user_message_count: int
    background_notification_count: int
```

`ObservationService.observe()` 返回该类型而非 `int`。

节点行为：

- 首次没有消息：设置 `requested_end=None`，转 `AWAIT_QUIESCENCE`；
- 有用户消息：`set_active(True)`；
- 只有后台通知：保持现有 active 值，不主动重新激活；
- 有任意消息：转 `BUDGET`；
- 后续 observe 理论上由 inbox activity 唤醒，但仍允许无消息竞态；无消息时转 `AWAIT_QUIESCENCE`，不忙循环。

`ObserveNode.allowed_targets = {BUDGET, AWAIT_QUIESCENCE}`。

### 7.3 `BudgetNode`

- `allowed_targets = {THINK, AWAIT_QUIESCENCE}`；
- stop 时构造 `ExecutionResult(presentation=budget_message)` 写入 `state.requested_end`；
- 转 `AWAIT_QUIESCENCE`，不直接 `End`。

### 7.4 `InferenceNode`

- 删除 `get_bg_pool` 构造参数和字段；
- `allowed_targets = {INTERPRET, AWAIT_QUIESCENCE}`；
- `InferenceCompleted`：转换 `ModelTurn`，转 `INTERPRET`；
- `InferenceStopped`：把当前 presentation/result 写入 `requested_end`，转 `AWAIT_QUIESCENCE`；
- 不再调用 `has_pending()`，不再知道 BackgroundTaskPool。

最终结构必须等价于：

```python
disposition = await self._inference.infer()
if isinstance(disposition, InferenceCompleted):
    state.turn = await self._current_channel().model_turn(self._inference_engine.result)
    return Transition(NodeId.INTERPRET)
state.requested_end = ExecutionResult(
    presentation=state.response,
    committed_output=state.committed_output,
)
return Transition(NodeId.AWAIT_QUIESCENCE)
```

这里不允许返回 `End`，也不允许查询 `get_bg_pool()`。

### 7.5 `ReActInterpretNode`

文件：`kernel/execution/graph/react.py`

- `allowed_targets = {ACT, AWAIT_QUIESCENCE}`；
- `FAIL` 仍抛错；
- `CONTINUE` 转 `ACT`；
- `VALIDATE_CANDIDATE` 将 `state.turn` 设置为 `PendingCandidate`，转 `AWAIT_QUIESCENCE`；
- 不直接转 `VALIDATE_OUTPUT`。

### 7.6 `ReviewInterpretNode`

文件：`kernel/execution/graph/review_refine.py`

- 同样把合法 final candidate 转成 `PendingCandidate -> AWAIT_QUIESCENCE`；
- tool action 仍 fail closed；
- ReviewRefine graph 也装配同一个 `AwaitQuiescenceNode`，不得建立第二套等待节点。

### 7.7 `ValidateOutputNode`

- 只允许从 `AWAIT_QUIESCENCE` 进入；
- 输入必须为 `PendingCandidate`；
- reject：原子记录 rejection/feedback，清空 candidate，转 `OBSERVE`；
- accept：调用一个新的 `OutputOperation.validate_and_commit()`，一次完成纯验证结果到原子终态事实提交；
- append 成功后设置 `state.response`、`state.committed_output`，直接 `End`；
- `allowed_targets = {OBSERVE}`。

## 8. 输出引擎与终态事务

### 8.1 收窄 `OutputEngine`

文件：`runtime/output/engine.py`

删除字段：

- `accepted_value`；
- `accepted_candidate_id`；
- `staged_output`；
- `committed_output`；
- `_lifecycle` 中 accepted/commit_started/committed 状态；
- `has_restored_terminal_output` 基于 accepted 的判断；
- `commit()` 两阶段实现。

保留能力：

- decode candidate；
- 顺序执行 validators；
- 维护 correction attempt；
- 生成 typed validation result；
- 严格恢复 committed terminal output；
- contract migration 在显式 migration 阶段执行，不在正常 decoder 中 fallback。

`evaluate()` 不得发布 accepted 事实。成功返回值应携带完整 commit 输入：

```python
@dataclass(frozen=True)
class ValidatedCandidate(Generic[OutputT]):
    candidate_id: str
    contract_id: str
    schema_fingerprint: str
    value: OutputT
    encoded_value: JsonValue
    correction_attempts: int
    validator_provenance: tuple[ValidatorProvenance, ...]
```

该类型属于 `contracts/output/models.py`，因为 Runtime transaction 和 Kernel output operation 都需要它；不得放在 Kernel 后让 Runtime 反向依赖。

### 8.2 新终态事件

文件：`contracts/events/output.py`

新增 `FinalOutputCommittedEvent`，单个事件内嵌最终消息：

```python
@dataclass(frozen=True)
class FinalOutputCommittedEvent(_DurableFact):
    candidate_id: str
    contract_id: str
    schema_fingerprint: str
    value: JsonValue
    message: Message
    correction_attempts: int
    validator_provenance: tuple[Mapping[str, JsonValue], ...]
    run_id: str
    run_kind: str
    fencing_token: int
```

选择单一组合事件而不是两个并列事件，原因：

- message 与 typed output 在协议上不可拆分；
- projection 重放不需要跨事件 join；
- 一次 append 天然提供原子边界；
- 恢复不存在“有 output 无 message”或“有 message 无 output”。

`message` 使用 canonical `encode_message/decode_message`，decoder 必须 exact-key、strict primitive、unknown-version fail closed。

删除生产事件：

- `OutputAcceptedEvent`；
- `OutputCommitStartedEvent`；
- `OutputCommittedEvent`；
- `OutputPublicationQueuedEvent` 作为 Session output lifecycle 事实。

`OutputCandidateReceivedEvent` 和 rejection 是否保留：

- candidate received 可作为恢复/审计事实保留，但不能被当作 accepted；
- rejection 保留，因为 correction budget 是 durable canonical state；
- migration event 仅在一次性 migration 工具中使用，迁移退出后删除生产 emit 路径。

### 8.3 替换 transaction Port

文件：`contracts/ports/execution/transaction.py`

删除 `OutputTransactionPort` 的：

- `stage_accepted_output()`；
- `commit_terminal_output()`。

新增：

```python
class OutputTransactionPort(Protocol[OutputT]):
    def context(...) -> ExecutionOperationContext: ...

    async def commit_final_output(
        self,
        context: ExecutionOperationContext,
        output: ValidatedCandidate[OutputT],
        message: Message,
    ) -> CommittedOutput[OutputT] | MutationResult: ...
```

语义：

1. 校验 run/fence/revision；
2. 构造单个 `FinalOutputCommittedEvent`；
3. 一次 `EventFabric.append()`；
4. append 成功后一次性更新内存 history projection；
5. 幂等清理 inference checkpoint；
6. 返回 `CommittedOutput`。

若事件已经以相同 operation id/candidate id 提交，返回同一个 `CommittedOutput`；不同 candidate 冲突 fail closed。

### 8.4 重写 `RuntimeExecutionTransaction`

文件：`runtime/persistence/execution_transaction.py`

- 删除 `_staged`；
- `_terminal` 只缓存已由 durable event 证明的结果；
- `_operations` 不能作为幂等真相，幂等查询必须来自 run-scoped Session projection/revision；它可以保留为读缓存，但 cache miss 必须查询 canonical store；
- `recover_frontier()` 改为只表达 committed terminal identity、revision 和 cancelled；删除 `staged_output_id`；
- `commit_final_output()` 在 append 前执行 fence guard，并让 fence 覆盖 append；
- checkpoint discard 在 committed event 之后执行；崩溃导致残留 checkpoint 时，恢复以 committed event 为准并幂等 discard。

## 9. Session 原子 batch 基础设施

### 9.1 扩展 SessionFactSink

文件：`contracts/ports/session/facts.py`

新增 typed batch：

```python
async def commit_facts(
    self,
    events: tuple[RolloutSourceEvent, ...],
) -> AppendResult: ...
```

要求：

- 空 batch 拒绝；
- 保持输入顺序；
- 全部编码成功后才调用 append；
- `EventFabric.append(stream_id, tuple(facts))` 是唯一 durable write；
- 任一 encode/append/fsync 失败时不更新内存 projection。

`commit_fact(event)` 可保留为一个事件用例，但内部调用 `commit_facts((event,))`，不是第二套实现。

### 9.2 修改 ContextManager

文件：`runtime/context/history/manager.py`

`add_batch()` 改为：

1. 过滤 `None`；
2. 为所有消息构造 `MessageAppendedEvent`；
3. 一次 `commit_facts(tuple(events))`；
4. 成功后使用一次 slice extension 更新 `_context.messages`；
5. durable commit 后逐个发 telemetry，telemetry 失败不得回滚 canonical history。

增加仅供 transaction 在“组合终态事件已经包含 message”后推进内存投影的窄方法，优先方案是让 Session projection subscriber推进；若当前 EventFabric 没有同步 projection subscriber，则定义明确的 `apply_committed_messages(messages)`，该方法不得写 Session，只接受由 transaction 返回的 typed append receipt。不得暴露裸 list mutation。

### 9.3 OBSERVE 原子 batch

`ObservationService.observe()`：

- `pop_all()` 后先去重；
- 调用新的原子 `add_batch()`；
- add_batch 失败时必须把消息按原 priority 恢复入 buffer，或者先使用 reservation/peek + commit + ack 模式，不能永久丢失已 pop 消息；
- 推荐给 `MessageQueue` 增加 typed drain lease：`reserve(max_priority) -> InboxBatchLease`、`ack(lease)`、`release(lease)`；
- 同一 lease/generation 下 commit 成功才 ack；失败 release 恢复原顺序。

## 10. THINK 原子写入

### 10.1 明确原子边界

模型网络调用不能与本地 Session append 构成同一个数据库事务。正确边界是：

```text
durable inference intent/checkpoint
-> provider call
-> durable provider result
-> 原子投影 model turn + checkpoint consumption
```

不得声称网络调用与 Session 写入原子。

### 10.2 具体改造

文件：

- `runtime/durable/inference_checkpoint.py`；
- `kernel/execution/operations/inference.py`；
- `runtime/persistence/execution_transaction.py`；
- model call projection store。

新增 `record_inference_projection()` transaction command，输入：

- model call identity/fence；
- provider result reference；
- `HistoryProjection`；
- checkpoint expected revision。

一次 transaction 必须：

- 原子追加 model-turn messages 和 checkpoint-consumed fact；
- 绑定相同 model call id、attempt id、fencing token；
- 重试相同 operation id 返回 already-applied；
- stale attempt fail closed。

当前 `record_model_turn()`、`record_tool_results()` 内部隐式 `record_result()` 的分散调用迁移到明确 command；迁移完成后删除隐式 checkpoint side effect。

## 11. ACT 原子写入与外部 effect 边界

外部 ToolCall 不能把“调用前 intent、远端动作、调用后 result”放入一个原子事务。必须拆成两个各自原子的 durable 阶段：

### 11.1 外部 effect 工具

```text
阶段 A（动作前原子）：
AI tool-call message + EffectIntent + approval/sandbox/fence identity

外部执行：
ToolExecutor 唯一 chokepoint

阶段 B（动作后原子）：
EffectReceipt/IN_DOUBT + ToolResult message + checkpoint settlement
```

远端动作发生而阶段 B 失败时，effect ledger 保持 `IN_DOUBT`，禁止假装未执行或盲重试。

### 11.2 非外部 effect 工具

无不可逆外部副作用时，可以把完整 model turn projection、tool results 和 checkpoint settlement 放入一个 Session batch，但仍必须保留 ToolExecutor 的 permission/effect audit identity。

### 11.3 具体接口

把 `record_model_turn()` 与 `record_tool_results()` 收敛为：

```python
async def record_effect_intent(..., projection, intents) -> MutationResult: ...
async def settle_effect_batch(..., projection, receipts, checkpoint) -> MutationResult: ...
async def record_local_action_batch(..., projection, checkpoint) -> MutationResult: ...
```

每个方法输入和返回都使用 canonical DTO，不传裸 dict、tool instance 或 live catalog。

## 12. Session codec、projection 与恢复

### 12.1 Codec

修改：

- `runtime/session/events.py`；
- `runtime/session/codec.py`；
- `contracts/events/output.py`。

新增唯一 tag，例如 `final_output_committed.v2`。编码/解码 exact fields；删除正常 decoder map 中的旧 accepted/commit-started/committed tags，待 migration 完成后旧 tag 只存在于离线 migration 工具的私有 decoder 中。

### 12.2 Projection

`runtime/session/projection.py` 对 `FinalOutputCommittedEvent` 一次执行：

- 把内嵌 AIMessage 加入 transcript projection；
- 设置 run-scoped terminal output projection；
- 记录 candidate/contract/schema/value/provenance/fence；
- status 只设为 `committed`；
- 不产生 accepted、commit_started、publication_queued 中间状态。

`OutputPublishedEvent` 只更新发布 receipt，不改变 Graph output canonical value。

### 12.3 SessionManager

`runtime/agent/session_manager.py`：

- 删除 `pending_output_restore` 对 accepted/commit_started 的筛选；
- 恢复结果改为 `restored_committed_output`；
- published output 不重新发布；
- committed 未 published output 由 Graph restore 返回相同 `ExecutionResult`，Product publisher 使用稳定 publication id 幂等投递。

`runtime/agent/role_state.py` 对应字段和 accessor 同步重命名；仓内消费者一次迁移后删除旧名。

## 13. 发布改造

当前 `Role.run()` 顺序：

```text
OutputPublicationQueuedEvent
-> drain
-> publish_message(rsp)
-> OutputPublishedEvent
-> drain
```

目标：

- 删除 Session output lifecycle 的 `publication_queued`；
- 引入/复用 Product-owned typed publication Port，输入稳定 `publication_id`、target、message 和 committed output reference；
- publisher 自己持久化 outbox pending/retry/ack/dead-letter；
- Role 得到 accepted receipt 后即可结束 Graph/run ownership；
- delivery ack 后写 `OutputPublishedEvent` 或 publisher-owned receipt projection；
- 重启时扫描 publisher outbox，不重新运行 Graph；
- `publish_message()` 只作为具体 routing adapter，不拥有 durable retry 状态。

若当前消息 routing port 没有 durable outbox，本切片必须补齐 canonical publisher，不能用 `try publish + Session queued flag` 继续冒充可靠发布。

## 14. Graph 装配修改

修改：

- `kernel/execution/operations/container.py`；
- `kernel/execution/graph/react.py`；
- `kernel/execution/graph/review_refine.py`；
- `kernel/execution/graph/__init__.py`；
- `kernel/execution/engine.py`。

`GraphAssemblyInputs`：

- 删除由 `InferenceNode`/`WaitBackgroundNode` 使用的后台等待依赖；
- 保留 `get_bg_pool`，仅注入 `AwaitQuiescenceNode`；
- 增加 `inbox_activity` 窄 Port provider；
- 不把完整 `ExecutionContext` 交给静默节点。

两个 built-in graph 都注册同一个 `AwaitQuiescenceNode`。Graph structure validation 必须证明：

- 无 `WAIT_BACKGROUND`；
- `INTERPRET -> AWAIT_QUIESCENCE`；
- `AWAIT_QUIESCENCE -> OBSERVE|VALIDATE_OUTPUT|End`；
- `VALIDATE_OUTPUT -> OBSERVE|End`；
- 除 committed restore、validate commit 和 quiescent no-candidate 外无 End 返回点。

## 15. 一次性 durable migration

### 15.1 位置与 owner

新增 Runtime Session-owned 离线 migration，例如：

```text
runtime/session/migrations/output_terminal_v2.py
```

Product composition 在 resume 前显式运行 migration。正常 Session decoder 不读取旧输出 tag。

### 15.2 输入识别

按 run id 聚合旧事件：

- `OutputAcceptedEvent`；
- `OutputCommitStartedEvent`；
- `OutputCommittedEvent`；
- 相邻 final AIMessage `MessageEvent`；
- `OutputPublicationQueuedEvent`/`OutputPublishedEvent`。

只迁移能够证明以下关系唯一的 run：

- candidate id 一致；
- contract/schema/value 一致；
- final AIMessage identity 唯一；
- committed fence 合法；
- published receipt identity 无冲突。

### 15.3 输出

- 写新版本 Session 文件或通过 versioned rewrite protocol 替换；
- 将完整旧链压缩成一个 `FinalOutputCommittedEvent`；
- published 事实保留为新 publisher receipt；
- 写 migration receipt：source digest、target digest、migration version、completed instant；
- write/flush/fsync/atomic replace/parent fsync；
- partial target 存在时按 digest 幂等继续或 fail closed。

### 15.4 退出条件

- 所有仓内 fixture 完成迁移；
- migration audit 证明无旧生产 Session；
- 删除正常生产旧 decoder、旧 projection 分支和旧事件 emit；
- `rg` 门禁禁止旧类型重新出现，migration 模块内的旧 wire decoder 使用独立、明确允许列表。

## 16. 实施顺序

### Phase 1：Contracts 与原子基础设施

1. 新增 generation-aware `MessageActivity`。
2. 修改 `MessageQueue` 与所有 activity consumers。
3. 新增 `ValidatedCandidate`、inference disposition、`FinalOutputCommittedEvent`。
4. 新增 `SessionFactSink.commit_facts()`。
5. 实现 ContextManager 真正原子 `add_batch()`。

完成门禁：contracts/runtime 单测、strict codec、batch append fault injection 通过。

### Phase 2：Graph 静默门

1. 新增 `AWAIT_QUIESCENCE`。
2. 删除 `WAIT_BACKGROUND`。
3. 迁移 Observe/Budget/Think/Interpret/ReviewInterpret 的边。
4. 增加 typed observation 和 inference disposition。
5. 两个 built-in graph 完成装配。

完成门禁：图结构、无消息结束、用户/后台双唤醒、active false、candidate invalidation 测试通过。

### Phase 3：最终输出单事务

1. 重写 OutputEngine 为 validate/reject/restore committed。
2. 实现 `commit_final_output()`。
3. 重写 ValidateOutputNode。
4. 删除 accepted/staged/commit-started 状态与事件。
5. 修改 projection/session manager/restore。

完成门禁：terminal event 与 AIMessage 不可拆分、恢复不重复模型调用、fence 和幂等测试通过。

### Phase 4：THINK/ACT 原子事实

1. checkpoint consumption 与 model projection 原子化。
2. external effect intent/receipt 两阶段各自原子化。
3. local action batch 单事务化。
4. OBSERVE 使用 inbox drain lease。

完成门禁：每个阶段故障注入后无半条消息、无丢 inbox、无伪完成 effect。

### Phase 5：发布与 migration

1. publisher outbox 接管 queued/retry/ack。
2. Role 删除 Graph-owned publication queued 状态。
3. 实现并运行 output terminal v2 migration fixture。
4. 删除旧 decoder/consumer/API。
5. 运行全量架构门禁与测试。

## 17. 测试改造清单

### 17.1 Graph 单测

- `ztest/flow/test_graph.py`：合法边、唯一节点、step bound；
- `ztest/flow/test_engine.py`：所有原 End 路径经过静默门；
- `ztest/flow/test_review_refine_graph.py`：共享静默节点；
- 新增 `test_await_quiescence.py`：generation race、pending task、用户消息、后台通知、candidate invalidation。

### 17.2 Observation/Inbox

- 同 batch 全有或全无；
- append 失败后 lease release，消息顺序与 priority 不变；
- 不再检查 recipient/watch；
- 后台通知不激活 stopped Role；
- 用户消息激活；
- generation 在 drain 后不回退；
- wait-after-generation 不丢 push。

### 17.3 Output

- candidate 等待静默前 decoder/validator 调用次数为 0；
- 静默后才调用 decoder/validators；
- 等待期间消息使 candidate 失效；
- reject 回 OBSERVE；
- accept 单 event 同时恢复 message/output；
- append、flush、fence 任一点失败都无内存 terminal projection；
- committed restore 直接返回相同 typed output；
- 不存在 accepted/commit_started 恢复路径。

### 17.4 THINK/ACT

- active false 不调用 provider，转静默门；
- inference result projection 与 checkpoint consumption 同事务；
- stale inference fence fail closed；
- effect intent 在外部动作前 durable；
- effect receipt 与 ToolResult 同事务；
- 本地 receipt 失败且外部动作已发生时为 IN_DOUBT；
- 多工具 batch 中 interrupted/skipped result 仍形成完整 typed settlement。

### 17.5 Session migration

- accepted-only、commit-started、committed、published 旧 fixture；
- 唯一 final message 成功迁移；
- 候选/消息歧义 fail closed；
- torn target 可幂等恢复；
- migration 重跑不产生第二 terminal event；
- 正常 decoder 不接受旧 tag。

### 17.6 Publication

- stable publication id；
- outbox accepted 后 Graph 不重跑；
- publish crash 后 publisher 重试；
- published ack 后不重复发送；
- 空 `send_to` 不投递；
- self+other 拆分且各一次。

## 18. 架构与搜索门禁

新增/更新 `ztest/architecture/` 门禁：

1. Kernel 不 import Runtime/Orchestration/Product。
2. `AwaitQuiescenceNode` 只依赖 Contracts Port。
3. `InferenceNode` 不引用 BackgroundTaskService。
4. `InferenceNode.allowed_targets` 必须精确等于 `{INTERPRET, AWAIT_QUIESCENCE}`，且 stopped 分支必须返回 `Transition(AWAIT_QUIESCENCE)`。
5. 生产源码不存在 `WAIT_BACKGROUND`、`WaitBackgroundNode`。
6. 生产源码不存在 `OutputAcceptedEvent`、`OutputCommitStartedEvent`、旧 `OutputCommittedEvent`。
7. 生产源码不存在 `stage_accepted_output`、`commit_terminal_output`。
8. `ContextManager.add_batch()` 只调用一次 batch commit。
9. `FinalOutputCommittedEvent` exact decoder 包含 message 和 output。
10. 正常 Session codec 不注册旧 output tags。
11. Graph 只有需求允许的 End 返回点。
12. 不新增局部 import、`Any` boundary、裸 dict DTO、`getattr/hasattr` 或动态 import。

建议 CI 搜索：

```text
rg "WAIT_BACKGROUND|WaitBackgroundNode|OutputAcceptedEvent|OutputCommitStartedEvent|stage_accepted_output|commit_terminal_output" contracts kernel runtime orchestration product
```

除一次性 migration 的隔离旧 wire decoder 外必须无匹配。

## 19. 完成定义

只有全部满足才算实施完成：

- 目标 Graph 拓扑与本文一致；
- final candidate 在静默前从未被验证或写入；
- 用户消息与后台通知只通过 message buffer activity 唤醒 Graph；
- 无 completion future 作为第二 Graph 唤醒链；
- OBSERVE/THINK/ACT/terminal output 的原子边界通过故障注入；
- committed final output 与 AIMessage 是单一 durable fact；
- 恢复不补 commit、不猜 ToolResult、不重复模型调用；
- 发布失败只由 publisher 重试，不重新运行 ReAct；
- 旧状态、旧事件、旧 API、旧 decoder 和旧测试全部退出；
- migration 可审计、幂等且无正常生产双读；
- Pyright、相关单测、全量测试和 `ztest/architecture/` 门禁通过；
- `git diff --check` 通过，且没有覆盖用户无关改动。
