# ADR-0003：Automation Trigger Port

- 状态：Accepted
- 日期：2026-07-29
- 决策 owner：Automation 与 Product Composition 评审

## 背景

当前 `CronService` 直接依赖 `AgentControl`、`UserMessage`、`DeliveryMode`，并读取 Agent runtime 的 idle 状态。将其移动到 `automation` 后继续调用 Agent facade，会永久绑定两个一级能力包。

## 决策

Automation 定义自己的输入 Port，四个 Orchestration 一级能力包零直接依赖：

```text
automation -> TriggerSink (consumer-owned Protocol)
product.composition -> AgentTriggerAdapter -> agents public command
```

稳定模型：

```text
AutomationTrigger
  trigger_id       稳定幂等键
  source_id        schedule/rule identity
  target           opaque target reference
  content          trigger payload（首版为文本）
  scheduled_at_ms
  fired_at_ms
  attempt

TriggerReceipt
  accepted | deferred | rejected
  receipt_id?
  reason?
```

`TriggerSink.dispatch(trigger) -> TriggerReceipt` 是唯一触发入口。Target 对 Automation 不透明；Automation 不构造 UserMessage，不选择 DeliveryMode，不读取 mailbox/runtime。

Idle/admission 属于 sink：若目标暂不可执行，sink 返回 deferred；Cron scheduler 根据 task policy 重试或记录，而不是遍历 Agent runtimes。首版 content 为文本是当前 Cron 产品语义，不使用任意 dict；未来出现第二种 payload 后再通过 tagged union 扩展。

`product.automation.AgentTriggerAdapter`：

- 解析 opaque target 到 Agent public reference。
- 将 content 映射为 Agent command/message。
- 选择 trigger-turn delivery policy。
- 将 Agent outcome 映射为 TriggerReceipt。
- 将 trigger ID 映射为未来 Agent command 的 `idempotency_key`。

## Delivery guarantee

当前 Agent `send_input` 接收边界没有 durable idempotency ledger，adapter 也没有能与 Agent 接收原子提交的持久化状态。因此本 ADR 诚实规定当前保证为 **at-least-once**：

- Cron store 保留 schedule/attempt，未确认 receipt 可以重试。
- 进程可能在 Agent 已接受、receipt 尚未持久化的窗口崩溃，导致重复触发。
- 内存 adapter dedupe 不构成 correctness guarantee，禁止宣称 effectively-once。
- `trigger_id` 是稳定幂等键，业务接收端应按该键实现幂等时才能获得 effectively-once effect。

未来若要求框架级 effectively-once，必须新增 Agent public `AgentCommand(idempotency_key=...)`，由 Agent owner 在 durable 接收边界原子完成 dedupe + enqueue，并返回可恢复 receipt；这需要独立 ADR。Automation 仍不依赖 Agent，Product adapter 只映射字段。

## 所有权与生命周期

- CronService 拥有 scheduler/store/lock。
- Adapter 代码归 `product.automation`；Product composition 构造并注入。
- shutdown 先停止新 trigger，再关闭 scheduler，最后关闭 Agent control plane。
- Adapter 不被 Automation 持久化。

## 拒绝方案

- `automation -> agents.AgentControl`：随 Agent API 演进。
- 把 TriggerSink 放进 `contracts/ports`：当前只有 Automation 消费，无需下沉。
- `payload: dict`：形成无 schema 逃逸口。

## 验收

- Automation 测试不 import `agents`、UserMessage 或 DeliveryMode。
- fake TriggerSink 可覆盖 accepted/deferred/rejected 和 retry。
- AgentTriggerAdapter 有独立映射与幂等测试。
- 崩溃窗口测试证明当前是 at-least-once；文档和 API 不声称 exactly/effectively-once。
- 四个一级 Orchestration 包直接 import 为零。
