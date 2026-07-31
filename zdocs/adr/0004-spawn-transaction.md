# ADR-0004：Spawn Transaction、取消与所有权转移

- 状态：Accepted
- 日期：2026-07-29
- 决策 owner：Agent Control Plane 评审

## 背景

Spawn 跨 admission、residency、identity、Role 创建、context provision、cost attribution、runtime/route 注册和 watcher。拆成 service 后若没有显式事务，很容易重复释放或暴露半注册 Agent。

## 决策

每次 Spawn 创建资源型 `SpawnTransaction`，状态只能单向推进：

```text
NEW -> ADMITTED -> RESIDENCY_RESERVED -> IDENTITY_RESERVED
    -> CHILD_BUILT -> PROVISIONED -> REGISTERED_INERT
    -> SUPERVISED -> RUNNABLE/COMMITTED
    -> ROLLED_BACK
```

### 输入契约

- `ParentSpawnSnapshot`：parent ID、path、cwd 和稳定策略输入；不可包含 Runtime Context/config object。
- `ChildFactory`：创建 child capability。
- `ChildContextProvisioner`：执行 context policy，不返回 Context。
- `CostAttributionPort`：预算视图、登记和释放，不暴露 CostTracker/CostNode。

这些 Port 默认归 `orchestration.agents.lifecycle`。只有低层也需要依赖时才提升到 `contracts/ports`，且 Contracts 不引用 Runtime 类型。

### Lease 与所有权

每个成功步骤登记一个 transaction-owned lease/reversal。Rollback 按获取逆序执行，并满足：

- 幂等；
- 一个 reversal 失败不阻止其余 reversal；
- 聚合 cleanup failure；
- `REGISTERED_INERT` 仅存在于 transaction 私有 staging，不进入公开 registry/route/scheduler；
- supervisor/TTL/completion subscription 在 inert child 上构造并安装，但尚不启动用户执行；
- `SUPERVISED` 后 commit/activate 将 runtime、route、cost attribution、residency ownership 一次性转给 Agent lifecycle owner，并使 child 首次 RUNNABLE。

Supervisor 安装失败必须 rollback；禁止让未受监管 child 进入 degraded-but-runnable 状态。Subscription 可以在 commit 前构造/登记到 inert source，但只有 activate 后接收事件，从而同时满足 commit 前不可见与首次执行前已监管。

### 取消

- 任意可等待步骤收到 `CancelledError` 后，transaction 在取消隔离下完成 rollback，再重新抛出取消。
- Commit/activate 是锁内、无 IO、无 await、不可取消的所有权转移临界区；它必须全量完成。
- 调用者在 commit 返回后取消，只能通过 handle/lifecycle release，不得调用 transaction rollback。

### Commit 中断

进程内 commit 通过锁内不可等待状态变更完成，不定义可恢复的 commit failure：违反内部不变量视为控制面故障。若未来涉及跨进程 durable registration，必须先新增 journal/reconciliation ADR；当前设计不提前引入模糊 reconciliation 状态，也不声称内存事务能回滚进程崩溃。

## 拒绝方案

- 多个 service 各自 try/finally：没有统一所有权。
- commit 后才安装 watcher：可能错过首次执行/完成。
- 捕获 `Exception` 处理取消：`CancelledError` 语义不可靠。
- rollback 静默吞掉失败：形成资源泄漏而无诊断。

## 验收

- 每个状态边界注入普通异常和 `CancelledError`。
- 所有 reservation、route、cost、runtime、watcher 计数回到基线。
- rollback/close/release 重复调用无重复释放。
- commit 前 child 不可寻址/调度；supervisor 安装后才能原子 activate；commit 后 transaction 不再拥有资源。
- MANAGED 与 EPHEMERAL 的 commit 后 owner 明确且分别测试。
