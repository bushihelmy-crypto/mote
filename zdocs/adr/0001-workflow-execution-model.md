# ADR-0001：Workflow 执行、结果与恢复模型

- 状态：Accepted（由 ADR-0006 补充五类终态与协作式停止协议）
- 日期：2026-07-29
- 决策 owner：Orchestration/Workflow 架构评审

## 背景

当前 `BgTaskResult` 同时表达前台值、deferred coroutine、后台提交、Graph metadata、恢复状态和模型展示。`resume()` 返回“即时提示 + 后续执行”的 hybrid descriptor，因此不能通过重命名为 `WorkflowResult` 完成解耦。

## 决策

Workflow 领域使用五个独立概念：

```text
WorkflowBuilder      可变声明器，只存在于构图阶段
WorkflowDefinition   build 后不可变、可并发复用的静态图定义
WorkflowRun          一次执行的唯一所有者和运行状态
WorkflowOutcome      Succeeded | Failed | Paused | Cancelled | TimedOut 封闭终态
WorkflowContinuation 从可恢复终态创建下一次 run 的不透明恢复能力
```

### WorkflowBuilder 与 WorkflowDefinition

- Builder 提供 `add_node/add_edge/...`，不允许执行。
- `build()` 复制 node/edge/topology，完成 topology、params、reducers、output contract 校验，生成稳定 definition ID/version，并冻结 Definition。
- Definition 拥有 frozen state schema、node definitions、topology、reducers 和 output contract。
- Definition 不保存 bound mutable builder；`build()` 后继续修改 Builder 不影响既有 Definition。
- build/validate 无 IO、无 task 创建。
- `start(input, *, progress_sink) -> WorkflowRun` 创建全新 run。
- 不保存 background task ID、Agent ID 或 presentation 文本。
- 同一 Definition 可安全并发创建多个 Run，任何 prepare/compile 派生数据都在 build 时冻结或归各 Run，禁止执行期原地修改 Definition。

### WorkflowRun

- 每次 start/resume 都有稳定 `run_id`。
- 独占 mutable run state、node records、attempt counters 和 cancellation scope。
- `execute() -> WorkflowOutcome` 至多由一个 owner 驱动；重复并发 execute 必须拒绝。
- `aclose()` 幂等并取消仍由该 run 拥有的节点任务。
- 前台调用者或 deferred operation 二选一拥有 run，禁止双重取消。

### WorkflowOutcome

```text
Succeeded(output, final_state_ref)
Paused(reason, resume_capability)
Failed(error, recoverability, resume_capability?)
Cancelled(reason, resume_capability?)
TimedOut(reason, resume_capability?)
```

- Outcome 是结构化领域事实，不含模型提示文本。
- 五类 outcome 都是一次 run 的终态，不代表整个 workflow definition 永久终止。
- `Paused` 可以携带 ADR-0006 定义的 decision/interaction request；后台 adapter 登记后只向 Pool 暴露 opaque resume/interaction reference。
- failure/cancel/timeout 不自动等价于 continuation；只有 ADR-0006 规定的明确可恢复状态才产生 ResumeCapability。

### WorkflowContinuation

- 由 Workflow owner 创建，封装 definition identity、execution revision、initial input reference、可选一致 checkpoint 和允许的 resume actions；restart-only capability 不伪造 state snapshot。
- 提供 `resume(request, *, progress_sink) -> WorkflowRun`。
- `request` 是结构化 `ResumeRequest`，覆盖 rerun/skip/from 等允许动作。
- Background pool 只能把 continuation 包装为 deferred operation，不得读取 graph ref、node state 或 run records。
- 若需要跨进程持久化，使用稳定 workflow definition ID + version + schema-versioned snapshot；禁止序列化 callable 或 Python module path。

## Deferred operation 边界

`background_tasks` 定义消费方协议和自己的 outcome：

```python
class DeferredOperation(Protocol):
    async def execute(self) -> OperationOutcome: ...
    async def request_stop(
        self,
        reason: StopReason,
        disposition: StopDisposition,
    ) -> OperationOutcome: ...
    async def aclose(self) -> None: ...
```

`StopReason = USER_CANCEL | TIMEOUT | SHUTDOWN | OUTPUT_CAP`；`StopDisposition = CHECKPOINT | DISCARD`。

`OperationOutcome` 是 background-owned `OperationSucceeded | OperationPaused | OperationFailed | OperationCancelled | OperationTimedOut`。除 Succeeded 外的终态按契约携带 optional opaque resume reference；Workflow continuation 由 `product.workflows` adapter 登记和持有。Pool 不得接收 `WorkflowOutcome`，不得 `isinstance`/属性探测/`str()` workflow 对象。

Continuation registry、inspection、opaque reference 和首版 process-local durability 由 [ADR-0006](./0006-bggraph-migration-contract.md) 固化。

实际接口可增加稳定 operation name 和 cancellation metadata，但不得增加 workflow 专属字段。`product.workflows.WorkflowTaskAdapter` 包装 `WorkflowRun` 并完成 outcome 转换；Product composition 只负责实例化/wiring，Workflow 不 import background tasks。

当前 hybrid 的即时提示由 Product Tool adapter 产生，deferred execution 作为独立 submission 提交。即时 acknowledgement 不是 WorkflowOutcome。

## DeferredOperation 生命周期

```text
CREATED -> EXECUTING -----------------> TERMINAL -> CLOSED
   |          |                            ^
   |          -> STOP_REQUESTED -> STOPPING|
   -> STOP_REQUESTED ----------------------|
```

- Operation single-use；第二次 execute 必须拒绝。
- Pool 从 submit 成功起独占 operation；submit 失败则所有权仍归调用方。
- Pool 对 user cancel/timeout/output cap 先调用 `request_stop()`，不得先 cancel execute task；operation 负责停止并 join 内部任务、冻结一致 checkpoint，并返回唯一 OperationOutcome。
- user cancel 与 timeout 默认使用 CHECKPOINT；session/Pool shutdown 因首版 registry 是 process-local，默认使用 DISCARD；明确 force-abort 一律 DISCARD。OUTPUT_CAP 的 disposition 由 submission policy 固定，默认 CHECKPOINT。
- submit 后、execute 前的 CHECKPOINT stop 可以从 initial input/definition 签发只允许 FULL_RESTART 的 capability；DISCARD 不生成 ref。
- execute 与 request_stop 竞争时由 operation 内部状态机线性化，只有一个调用提交并返回同一个 terminal outcome；另一调用观察该 outcome，不得生成第二终态。
- Pool timeout 只由自己的 monotonic deadline 触发一次 request_stop；禁止同时使用 `asyncio.wait_for` 再制造第二次 timeout/cancel。
- request_stop 超过有界 grace period仍不合作时，Pool 才强制 cancel/join execute task；强制路径只能生成无 ResumeRef 的 background-owned Cancelled/TimedOut outcome。
- DISCARD 永远不生成 ResumeRef；CHECKPOINT 也只有在 operation 成功冻结恢复必需状态后才能生成。
- execute 正常返回或抛出异常后仍必须 aclose；所有终止路径最终恰好触发一次逻辑 close。
- 强制 cancel 或外部 `CancelledError` 后 cleanup 必须 shield，随后保留取消语义。
- resubmit 使用 `DeferredOperationFactory` 创建新实例；不得复活 TERMINAL/CLOSED operation。
- WorkflowRun 的所有节点 task 必须在 operation close 返回前结束。

## 拒绝方案

- 将 `BgTaskResult` 原样搬进 `workflows`：保留错误 owner。
- 让 Pool 接收 `GraphMeta`：把 import 耦合改成对象探测。
- 让 Pool 接收 `WorkflowOutcome`：运行时仍耦合 workflow 领域。
- 把 pause 当异常：丢失正常可恢复终态语义。
- 序列化 graph object/callable：模块移动和重启不稳定。

## 验收

- 前台 run 和经 adapter 后台 run 产生相同 WorkflowOutcome。
- Builder build 后的 Definition 不受 Builder 后续修改影响，且并发 runs 不共享 mutable state。
- pause 后原 run 已终止；continuation 创建新 run。
- Pool 测试不 import workflow 类型，也不读取 workflow attribute。
- operation 双重 execute、execute/stop race、pre-start stop、graceful/forced timeout/cancel/shutdown、全部 close 和 factory resubmit 有确定行为。
- resume/skip/from 的现有行为由 ResumeRequest 等价覆盖。
