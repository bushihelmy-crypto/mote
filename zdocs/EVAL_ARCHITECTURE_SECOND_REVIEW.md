# Mote Eval 架构第二轮评审

> 评审对象：[`EVAL_PACKAGE_SKELETON.md`](./EVAL_PACKAGE_SKELETON.md)
>
> 前置评审：[`EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md`](./EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md)
>
> 评审目标：继续验证 Eval 目标设计与 Mote 现有 Event、Artifact、Application、Permission、Inference 和持久化契约的兼容性，识别实现前仍未闭合的长期边界。

## 1. 结论

第二轮结论仍为：**有条件通过，禁止直接进入实现。**

第一轮修订已经解决了多数表层问题，但进一步对照现有源码后，发现主设计仍在重新定义 Mote 已经拥有的基础契约，并在 execution ownership、attempt lifecycle、artifact retention 和 journal repair 上存在新的阻断矛盾。

最关键的判断是：

> Eval 应新增实验领域状态机和 reducer，不应新增第二套通用 EventEnvelope、EventJournal、ArtifactRef 或 application lifecycle 基础设施。

当前必须优先关闭的七个阻断问题：

1. EvalEventEnvelope 和 durable journal 与现有通用 Event Fabric 重复。
2. CaseExecutor 实际承载 attempt，命名、请求和所有权粒度不一致。
3. lifecycle 声称 attempt-scoped，状态机却把 setup/teardown 放在 retry 循环之外。
4. Eval ArtifactRef 重复已有契约，直接复用现有 ArtifactRef 又缺少 experiment retention/ownership 映射。
5. 主设计要求自动忽略 torn tail，与现有 journal 的严格完整性策略冲突。
6. cleanup 同时混合 task hook cleanup 与 executor 强制资源回收，无法证明超时后的真实释放状态。
7. fixture path、摘要规范和 provenance secret fingerprint 尚未形成安全、确定的 canonical boundary。

这些问题解决后，Eval 才能建立在 Mote 的正式基础设施之上，而不是成为一套平行 runtime。

## 2. 已存在的通用 Event 契约必须复用

### 2.1 当前源码事实

Mote 已经提供：

- `contracts/events/envelope.py::EventEnvelope`；
- `contracts/ports/events/journal.py::EventJournal`；
- `UncommittedFact`、`AppendResult`、`VerificationReport`；
- `runtime/events/journal.py::LocalEventJournal`；
- journal-assigned sequence 和 recorded time；
- expected-version CAS；
- event ID 唯一性；
- checksum chain；
- 每次 append 的 flush + fsync；
- 新文件目录 fsync；
- verified snapshot read；
- session live/replay 共用 reducer 的既有模式。

这套能力已经覆盖主设计中 EvalEventEnvelope 和大部分 journal infrastructure 的目标。

### 2.2 主设计的重复

主设计重新定义：

```python
class EvalEventEnvelope:
    schema_version
    sequence
    event_id
    experiment_id
    case_id
    run_id
    attempt_id
    occurred_at
    event_type
    payload
```

如果照此实现，会产生：

- 两套 envelope validation；
- 两套 sequence 分配；
- 两套 event ID 类型；
- 两套 JSON freeze/thaw；
- 两套 journal checksum 和完整性规则；
- 两套 optimistic concurrency；
- 两套 fsync、verified read 和 projection cursor 语义。

这是十年架构中的直接负债，必须在编码前消除。

### 2.3 建议边界

Eval 只定义领域 payload 和 codec：

```python
EvalFactPayload = Union[
    ExperimentDeclared,
    CaseDeclared,
    AttemptScheduled,
    AttemptCompleted,
    EvaluatorCompleted,
    ...,
]
```

通用 envelope 使用：

```python
EventEnvelope[Mapping[str, JsonValue]]
```

映射规则：

```text
stream_id       = eval.experiment.<experiment_id>
event_type      = eval.<aggregate>.<fact-name>
run_id          = Eval RunId
correlation_id  = case/run/attempt 因果关联
causation_id    = 触发本事实的前序 EventId
payload         = experiment/case/run/attempt 的完整稳定 identity
metadata        = 仅放小型非领域索引，不承载状态事实
```

`EventEnvelope` 当前没有 case_id/attempt_id 顶层字段，不应因此扩展通用 envelope。两者属于 Eval payload；需要查询索引时由 Eval projection 建索引。

### 2.4 Infrastructure 注入

Eval application 应依赖 `contracts.ports.events.EventJournal`，而不是 import `runtime.events.LocalEventJournal`。

本地 composition 由 Product/Eval host 注入现有 LocalEventJournal。建议提供窄工厂：

```python
class EvalJournalFactory(Protocol):
    def open(self, experiment_id: ExperimentId) -> EventJournal: ...
```

该工厂属于 Mote host facade；Eval domain/application 不认识 runtime 实现。

## 3. Torn tail 与 journal repair 策略冲突

### 3.1 当前源码行为

现有 `LocalEventJournal` 将以下情况视为完整性失败：

- 尾部没有换行；
- JSON 不完整；
- sequence 不连续；
- checksum chain 断裂；
- event ID 重复；
- envelope shape 不匹配。

它不会自动忽略或截断 torn tail。验证失败后，append 也不能继续。

### 3.2 主设计当前承诺

主设计要求：

> 最后一个无换行或 JSON/schema 不完整的尾部记录视为 torn tail，忽略并记录 recovery diagnostic。

该策略与现有 journal 不兼容，而且“读取时忽略，随后继续 append”会使物理尾部仍然存在，下一次扫描继续失败。

### 3.3 推荐策略

默认保持严格完整性：

- verify 发现 torn tail 后实验进入 `integrity_blocked`；
- show/verify 可以展示最后一个已验证 sequence，但不能宣称完整 replay；
- runner 不得自动 append 或 resume；
- 显式 `mote.eval repair` 才能执行修复。

repair 流程：

```text
锁定 experiment stream
→ 复制原 journal 为 PINNED/SECRET artifact
→ 验证损坏只发生在最后一条 torn record
→ 截断到最后一个 verified byte offset
→ fsync file + directory
→ 重新 verify
→ 在新追加事实中记录 JournalTailRepaired
   （source digest、removed bytes digest、operator/recovery policy）
```

中间记录损坏、checksum 冲突或 sequence gap 不允许自动修复。必须迁移或人工处置。

主设计应删除“自动忽略 torn tail”的承诺，改为“可验证、显式、留证的 repair”。

## 4. CaseExecutor 的粒度错误

### 4.1 当前矛盾

主设计命名为 `CaseExecutor`：

```python
execute(CaseExecutionRequest)
cancel(ExecutionLease)
reconcile(ExecutionLease)
```

但 request 包含 `attempt_id`，retry 为每次 attempt 创建新进程、workspace、session 和 lease。实际执行与取消粒度显然是 attempt，不是 case。

同时，ExperimentRunner/CaseMachine 负责：

- retry；
- selected attempt；
- prepare evaluation；
- evaluators；
- case teardown/finalize。

这意味着 executor 并不拥有整个 case 生命周期。

### 4.2 长期风险

如果保留 CaseExecutor 名称，会持续产生以下歧义：

- 一个 case 是一个 lease，还是每个 attempt 一个 lease；
- evaluator 在 worker 内执行还是 coordinator 执行；
- retry 是 executor 内部行为还是 CaseMachine 行为；
- cancel case 是否取消当前 attempt、后续 retry 和 evaluator；
- reconcile 返回 attempt 状态还是 case 状态；
- remote executor 的幂等键究竟绑定哪个 identity。

### 4.3 建议拆分

将端口改为 `AttemptExecutor`：

```python
class AttemptExecutor(Protocol):
    async def start(self, request: AttemptExecutionRequest) -> AttemptLease: ...
    async def cancel(self, lease: AttemptLease, reason: CancellationReason) -> CancellationReceipt: ...
    async def reconcile(self, lease: AttemptLease) -> AttemptExecutionStatus: ...
```

职责：

```text
CaseMachine
  拥有 case/run/retry/evaluator/finalization

AttemptExecutor
  拥有一次 task attempt 的 worker、session、workspace execution 和强制回收
```

如果未来确实需要把完整 case 远程化，应新增更高层 `CaseWorker` 协议，不能让一个名字在不同部署模式下改变所有权语义。

## 5. Attempt lifecycle 与 Retry 状态机冲突

### 5.1 当前矛盾

主设计声明：

- Lifecycle 是 attempt-scoped；
- 每次 retry 使用全新 workspace；
- setup receipt 记录 attempt 资源；
- teardown 释放 attempt 资源。

但正式 phase 顺序是：

```text
workspace_provisioning
→ setup
→ task_attempts（内部可多次 retry）
→ prepare_evaluation
→ evaluators
→ teardown
```

这会让所有 retry 共享 setup 和 teardown，与 fresh-attempt 保证直接冲突。

### 5.2 推荐双层状态机

Case 与 Attempt 必须分离：

```text
CaseMachine
  declared
  → attempts
      AttemptMachine #1
      AttemptMachine #2
      ...
  → select_attempt
  → prepare_evaluation
  → evaluators
  → case_finalize

AttemptMachine
  scheduled
  → workspace_provision
  → setup
  → task_execute
  → task_teardown
  → executor_reclaim
  → attempt_finalize
```

每次 retry 都完整运行一个 AttemptMachine。

只有 selected successful attempt 的 immutable output/workspace snapshot 会进入 prepare-evaluation。若 evaluator 需要真实 workspace，attempt workspace 必须被 lease 保留到 evaluation 完成；但 task process 和其他运行资源仍应在 attempt teardown/reclaim 中关闭。

这要求将资源拆成：

- execution resources：Agent、进程、Terminal、后台任务；attempt 完成后立即释放；
- evaluation material：只读 workspace snapshot、output 和 artifacts；保留至 evaluator 完成；
- case resources：journal/projection 等 case-level 能力；case finalization 后释放。

## 6. Cleanup 语义仍未闭合

### 6.1 两类 cleanup 被混合

主设计只有一个 `cleanup_status`，但实际存在两个不同责任主体：

```text
TaskLifecycle teardown
  被测任务的正常、协作式资源释放。

AttemptExecutor reclaim
  父进程/外部 supervisor 对 worker、进程组和残留资源的强制回收与验证。
```

超时或 kill 后，worker 内 teardown 可能永远无法执行；此时不能因为父进程杀掉 worker 就将 teardown 记为 succeeded。

### 6.2 建议状态

```text
teardown_status:
  pending | running | succeeded | failed | not_reached | worker_lost

reclamation_status:
  pending | running | succeeded | partial | failed | unverifiable
```

Case 的 cleanup health 由二者投影，但不能覆盖原始维度。

`reclamation_status=succeeded` 必须有父级证据：

- worker PID/identity 已终止；
- process group/cgroup 无存活成员；
- Product facade close receipt；
- session singleton ownership 已释放；
- workspace writable lease 已关闭；
- artifact/journal writer 已完成或明确失败。

单纯 `proc.kill()` 或等待 exit code 不足以证明全部后代资源被回收。

## 7. Artifact 复用仍需 ownership bridge

### 7.1 第一轮意见需要进一步收紧

第一轮正确指出不应重复定义 ArtifactRef，但“直接复用”仍不完整。

现有 Artifact ownership 只有：

```text
SESSION  → session_id
PROJECT  → project_id
PINNED   → global
```

Eval 需要：

- attempt 临时 artifact；
- run/case 结果 artifact；
- experiment report/provenance artifact；
- repair evidence；
- 实验完成后的 retention/release。

直接使用 SESSION 会在 attempt 关闭后失去报告依赖；直接使用 PROJECT 又必须明确 project owner 如何映射到 experiment。

### 7.2 推荐映射

不修改通用 ArtifactRetention 枚举，不加入 Eval 专有枚举值。由 Product host 为 experiment 提供 scoped ArtifactStore：

```text
ArtifactOwnership.session_id = attempt_id
ArtifactOwnership.project_id = stable experiment artifact owner ID

EPHEMERAL/SESSION  用于 attempt 内可丢弃产物
PROJECT            用于 experiment journal/report 引用的产物
PINNED             仅用于显式保留、repair evidence 或 legal hold
```

Eval 定义 `EvalArtifactRecord` 记录 experiment/case/run/attempt/producer 归属，但不重定义 content、retention 或 sensitivity。

还必须定义：

- selected attempt 前，哪些 attempt artifact 晋升到 PROJECT；
- 未选中 attempt artifact 的默认保留期；
- report 引用 artifact 后的 reachability root；
- experiment 删除时如何 release PROJECT ownership；
- repair evidence 为什么及何时 PINNED；
- Eval projection 如何报告 missing/corrupt，而不修改 ArtifactRef 历史事实。

### 7.3 注入边界

Eval adapter 不应直接 import `runtime.artifacts.ArtifactRepositoryLayout`。Product headless host 应提供 scoped ArtifactStore/Resolver/Publisher ports。

## 8. Durability policy 与现有实现不一致

主设计提供 strict/balanced/relaxed 三档，但现有 LocalEventJournal 每次 append 都 fsync，没有 per-append durability 参数。

如果为了兑现 balanced/relaxed 而另写 Eval journal，会重新制造基础设施。建议第一阶段：

- journal 固定使用现有 strict durable append；
- projection 可使用独立、明确的异步/原子写策略；
- 不对外暴露尚无实现支持的 durability mode；
- 未来若证明确需批量 durability，在通用 EventJournal port 上通过 ADR 增加 capability negotiation，而不是 Eval 私有扩展。

“phase boundary durable”可以通过批量 append 一组 facts 实现，但一个 batch 内事实会一起提交，不应假装每条具有独立 crash boundary。

## 9. Heartbeat 不应进入 experiment journal 热路径

主设计把 `TaskAttemptHeartbeat` 放进正式事实集合，同时要求单 experiment journal 单写者。

高频 heartbeat 进入 append-only journal 会带来：

- fsync 放大；
- journal 膨胀；
- 多 case 并发争夺全局 sequence；
- coordinator 死亡时 worker 无法 durable ack heartbeat；
- replay 被大量非领域状态污染。

建议分离：

```text
AttemptLeaseStore / supervisor
  保存带 TTL 的当前 liveness、worker identity 和 heartbeat；属于控制面可覆盖状态。

Experiment journal
  只记录 lease acquired、lease lost、reconcile decision 和 terminal receipt；属于不可变事实。
```

LocalProcess executor 可以直接通过父子进程和 PID/cgroup 检测 liveness，不需要 durable heartbeat。Remote executor 才需要 LeaseStore port。

## 10. Canonical digest 尚未定义

主设计大量依赖 digest，但没有定义唯一 canonical encoding。`sort_keys=True` 本身不足以形成十年稳定契约，尤其涉及：

- Unicode normalization；
- float 表示；
- `-0.0`；
- int 边界；
- tuple/list；
- map key 顺序；
- Decimal；
- datetime 和 timezone；
- Path；
- codec version；
- 缺失字段与显式 null。

建议定义 `CanonicalValueV1`：

- 输入先经过 ValueCodec，输出受 `contracts.events.JsonValue` 同等约束；
- 字符串统一 NFC；
- object key 按 Unicode code point 排序；
- UTF-8、`ensure_ascii=False`、无额外空白；
- int 限制 signed 64-bit；
- 第一版 digest boundary 禁止 float，scalar metric 的 float 不参与 identity digest，或使用明确十进制字符串 codec；
- datetime 转为 UTC RFC3339，固定微秒策略；
- Path 禁止进入 canonical value；必须先解析为 ArtifactRef/tree manifest；
- digest 输入包含 canonical format ID 和 codec ID/version；
- 输出使用现有 `ContentDigest`，不要再造 Digest 类型。

必须提供 golden vectors，保证未来 Python 版本和其他语言实现得到相同摘要。

## 11. Fixture 的声明路径与 durable 形式冲突

Python 模型将 fixture 表达为 ArtifactRef，YAML 示例却使用：

```yaml
fixture: fixtures/web-empty
```

这两个阶段必须明确分开：

```text
DatasetSource
  可包含相对配置文件的本地路径，仅存在于不可信加载边界。

DatasetMaterializer
  校验路径、符号链接、文件数量/大小和 ignore policy；
  生成规范化 tree manifest + content artifacts。

MaterializedDataset
  只包含 ArtifactRef、tree digest 和 codec 化值；
  这是 ExperimentDeclared 与 dataset_digest 的唯一输入。
```

原始本地绝对路径不能进入 dataset digest 或 durable result。路径只可作为脱敏 source observation 保存。

还需定义 tree manifest：相对 POSIX 路径、entry kind、mode policy、content digest、size、symlink policy、排序和大小上限。

## 12. Secret fingerprint 存在泄漏风险

主设计要求记录 secret 的“不可逆脱敏 fingerprint”。对低熵 token、短密码或已知候选值直接做 SHA-256，仍可离线枚举。

建议：

- 默认只记录 secret source ID、provider-side version ID 和是否存在；
- 若必须判断同一部署内 secret 是否变化，使用由本机 secrets root 管理的 keyed HMAC；
- HMAC key ID 进入 provenance，key 本身不进入实验；
- HMAC 不能跨信任域用于公开比较；
- 没有 provider version/HMAC 时标记 provenance unknown，不对 secret value 做裸 hash；
- endpoint 记录必须移除 userinfo、query、fragment 和 tenant secret；
- dirty diff、环境快照和配置诊断 artifact 默认 sensitivity=SECRET，并经过显式 redaction 后才允许报告展示。

## 13. Version 与历史解码职责混合

主设计要求注册表精确匹配 `(kind, stable_id, version)`，这是执行时正确策略，但历史读取不能依赖旧 evaluator/task 实现仍安装。

必须分离：

```text
ExecutionRegistry
  解析当前可执行的 Task/Evaluator/Policy 实现；版本不匹配拒绝新实验。

SchemaDecoderRegistry
  解码历史 event/result/config schema；受长期兼容和 migration policy 管理。
```

CLI show/report/replay 只能依赖 domain schema decoder 和 reducer，不能 import 或实例化历史 evaluator。否则删除一个 evaluator 插件会导致旧实验无法查看。

还需定义：

- event schema support window；
- migrator 的稳定 ID、source/target version；
- reducer version 与 projection version；
- migration 是否可逆；
- 旧 journal 永不原地修改；
- opaque future event 导致的 projection capability 状态。

## 14. ID 生成与幂等规则不足

当前只列出 ID 类型，没有定义生成和冲突规则。

建议：

- ExperimentId：不可预测随机 ID，创建前 durable reservation；
- DatasetId/CaseId/EvaluatorId/MetricId：作者声明、命名空间化、格式受限；
- RunId：由 ExperimentId + CaseId + repeat index 确定派生，重复调度得到同一值；
- AttemptId：由 RunId + attempt index 确定派生，必须先 durable `AttemptScheduled` 后启动；
- EventId：由 operation ID + fact kind 确定派生或由 producer 持久保存，重交保持一致；
- Execution ID：等于 AttemptId，避免第四个同义身份；
- ID 重新使用但 config/request digest 不同是幂等冲突，不能覆盖。

随机 ID 只适合独立聚合根；可重试子操作应优先确定派生 ID。

## 15. Selected attempt policy 未定义

主设计保留所有 attempts 并指向 selected attempt，但没有定义选择规则。至少需要版本化 `AttemptSelectionPolicy`：

```text
first_success
  选择第一个 execution succeeded 且 output codec 成功的 attempt。

last_terminal
  选择最后一个 terminal attempt；仅用于显式续跑/诊断。

none_on_failure
  全部失败时不选择 output，evaluators 全部 skipped。
```

默认应为 `first_success + none_on_failure`。Evaluator 不得在多个 attempts 中自行挑选“最好结果”，否则 retry 会变成隐式 best-of-N。

## 16. 时间与顺序语义未定义

并发实验不能把 journal sequence 当业务时间。需要：

- recorded_at：journal commit UTC 时间；
- occurred_at：producer UTC observation，可能延迟或乱序；
- duration：同一 worker 内 monotonic clock 计算；
- timeout：基于 executor monotonic deadline；
- deadline：绝对 UTC 只用于跨进程传递，并由接收方换算为 monotonic budget；
- report 稳定排序：dataset case order、repeat index、attempt index、evaluator declaration order，而不是事件到达顺序。

建议通过 Clock port 注入测试时间。禁止用 wall clock 差值计算 duration。

## 17. CaseResult digest 的可变健康状态问题

Artifact missing/corrupt、projection freshness 和外部 provider health 会随时间变化。如果 `CaseResultFinalized` digest 包含这些可变观察，历史终态会因后续 GC 或检查而变化。

应分离：

```text
CaseResultFact
  只包含运行结束时的不可变 refs、metrics、receipts 和状态。

CaseHealthProjection
  当前 artifact availability、projection freshness、external reachability；可重建、可变化。
```

`CaseCompleted` 只引用不可变 CaseResultFact digest。`verify` 命令产生新的 integrity observation，不重写历史 result。

## 18. 进一步建议的目标边界

```text
contracts
  EventEnvelope / EventJournal port / ArtifactRef / ContentDigest

product public host facade
  HeadlessCodingApplication
  PolicyBoundCommandService
  EvalJournalFactory
  scoped ArtifactStore/Resolver

eval/domain
  Dataset/Case/Metric/Provenance
  Eval fact payload union
  CaseMachine + AttemptMachine reducer
  EvalArtifactRecord

eval/application
  ExperimentRunner
  AttemptExecutor port
  AttemptLeaseStore port
  WorkspaceSnapshot port
  recovery/reconcile policies

eval/infrastructure
  LocalProcessAttemptExecutor
  fixture materializer
  projections/report store

eval/adapters/mote
  MoteCodingTask
  Product host facade adapters
```

这套边界保留 Eval 自己的领域语义，同时复用 Mote 已有的通用 durable primitives。

## 19. 新增实现前门槛

在现有验收门槛基础上，增加：

1. Eval 不定义第二套 EventEnvelope、EventJournal、ArtifactRef 或 ContentDigest。
2. Eval event 使用现有 EventJournal port；本地实现通过 composition 注入，不由 domain/application import runtime。
3. journal 损坏默认 fail-closed；torn tail 只能经显式、有证据的 repair 修复。
4. `CaseExecutor` 改为 attempt 粒度，或以 ADR 证明为何 executor 拥有完整 case。
5. 每次 retry 完整执行 AttemptMachine，包括 workspace、setup、task teardown 和 executor reclaim。
6. task teardown 与 executor reclamation 使用不同状态和 receipt。
7. ArtifactRef 使用现有 Contracts 类型，并定义 experiment owner/retention/reachability 映射。
8. 第一阶段 journal 固定使用已有 strict fsync，不暴露虚假的 balanced/relaxed 模式。
9. heartbeat 位于 lease control plane，journal 只记录所有权和 reconcile 事实。
10. CanonicalValueV1 有正式规范和 golden vectors。
11. fixture path 在 ExperimentDeclared 前物化为 tree manifest 与 ArtifactRef。
12. secret 不做裸 hash fingerprint；使用 provider version 或 keyed HMAC。
13. ExecutionRegistry 与历史 SchemaDecoderRegistry 分离。
14. RunId、AttemptId、EventId 的确定派生和幂等冲突规则明确。
15. AttemptSelectionPolicy 显式版本化，默认不实施 best-of-N。
16. duration 使用 monotonic clock，report 顺序不依赖 journal 到达顺序。
17. immutable CaseResultFact 与 mutable health projection 分离。
18. phase crash test 同时覆盖 coordinator 崩溃、worker 崩溃和两者失联。

## 20. 最终判断

主设计已经具备完整 Eval 领域意识，但仍有把 Eval 做成“第二套 Runtime”的倾向。Mote 当前已经拥有可复用的 Event、Journal、Artifact、Application 和部分 reconciliation primitives；十年零负债的正确做法是补齐公开门面并复用这些契约，而不是在 `eval/` 下重新实现同义基础设施。

下一轮修订的核心不应继续增加类，而应完成三次删减和两次拆分：

```text
删减：EvalEventEnvelope、Eval ArtifactRef、Eval 私有 journal 基础协议

拆分：
CaseMachine / AttemptMachine
Task teardown / Executor reclamation
```

完成这些修订后，文档才接近可以进入 Phase 1 实现的“单一事实、单一所有权、单一基础设施”状态。
