# ADR-0002：Workflow Progress 与 Presentation 边界

- 状态：Accepted
- 日期：2026-07-29
- 决策 owner：Workflow、Background Tasks 与 Product Presentation 联合评审

## 背景

当前 graph progress、后台任务通知、Agent wake、模型文本渲染交织在 `report.py`、`notify.py` 和 task pool 中。物理搬迁不能解决事实、交付和展示的 owner 冲突。

## 决策

固定四层：

```text
WorkflowEvent       Workflow 执行事实，workflows 拥有
ProgressSink        Workflow 定义的输出 Port
TaskEvent           后台任务生命周期事实，background_tasks 拥有
Presentation        模型/UI 文本与本地化，Product 拥有
```

### WorkflowEvent

带显式 `schema_version` 的封闭 tagged union，至少覆盖 run started/terminal、node started/succeeded/failed/skipped、retry、pause 和 route decision。字段只使用 workflow-owned 稳定 ID、状态、结构化 error/reference 和时间；不含预渲染文本、UserMessage、Task ID 或 UI 类型。跨层 error 使用 contracts-owned stable error code/ref，不暴露 Python exception module identity。

事件说明已经发生的事实，不是控制命令。事件背压、丢弃策略和持久性由注入 sink 的契约明确，Workflow 不查找 ambient global writer。

### ProgressSink

由 Workflow 定义最窄输出能力：

```python
class ProgressSink(Protocol):
    async def emit(self, event: WorkflowEvent) -> None: ...
```

固定语义：

- `emit()` 是 async。
- 单一 WorkflowRun 内事件严格保序；不同 runs 无全局顺序承诺。
- 默认 null sink 允许 Workflow 独立运行。
- sink policy 在 Run 创建时固定为 `DURABLE` 或 `OBSERVATIONAL`，运行中不可替换。
- Durable sink 施加背压；emit 失败使 run 以明确 `ProgressDeliveryFailure` 失败，原始业务失败作为 cause 保留。
- Observational sink 可按声明的 bounded overflow policy 丢弃，但必须累计并暴露 dropped count；sink 自身异常不能改变 WorkflowOutcome。
- terminal event 必须先按 policy 完成交付，`execute()` 后返回 outcome；调用方看到 outcome 时不会再收到该 run 的后续事件。
- sink callback 禁止调用 pause/resume/cancel/execute 等控制同一 WorkflowRun 的方法；检测到重入应失败而非死锁。

### TaskEvent

使用 background-owned tagged union，只表达 `submitted/started/succeeded/paused/failed/cancelled/timed_out`、result pointer 和 delivery state。五类 terminal event 与 OperationOutcome 一一对应；非成功 terminal event 只可携带 optional opaque ResumeRef，不能携带 Workflow capability。它可以引用 opaque workflow run ID，但不复制 node graph state。

Background Tasks 负责 task terminal delivery 的幂等和 Agent wake adapter 调用，不渲染面向模型的 stage summary 或 error prose。

### Product Presentation

Product projector 接收 WorkflowEvent/TaskEvent，生成模型 reminder、CLI activity、日志或远程协议映射。本地化、XML/text rendering、stage summary prose 和“resuming/skipping”提示全部归 Product。

## 失败策略

- 关键 sink 若声明 durable/backpressured，emit 失败按 run policy 处理。
- loss-tolerant observation sink 可以丢弃，但必须暴露 dropped count。
- Presentation 失败不能改写 WorkflowOutcome；交付失败由 Task delivery 单独记录。

## 拒绝方案

- `workflows.progress.rendering`：把用户展示留在领域层。
- Workflow 直接发送 BackgroundTaskNotification：形成反向依赖。
- 用自由文本作为唯一 progress payload：无法稳定投影或演进。

## 验收

- Workflow 在 null/fake sink 下完整运行。
- Durable sink 背压、失败、terminal-before-return 和严格顺序有测试。
- Observational sink overflow/dropped count 和 sink exception isolation 有测试。
- 同一 run sink callback 重入被拒绝。
- Product 未导入时不产生模型/UI 文本。
- WorkflowEvent 与 TaskEvent 分别有唯一 owner 和 schema/tag 测试。
- terminal task delivery 重复生产时仍只交付一次。
