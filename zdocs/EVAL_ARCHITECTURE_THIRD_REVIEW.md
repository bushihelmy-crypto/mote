# Mote Eval 架构第三轮评审

> 评审对象：[`EVAL_PACKAGE_SKELETON.md`](./EVAL_PACKAGE_SKELETON.md)
>
> 前置评审：
>
> - [`EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md`](./EVAL_CURRENT_IMPLEMENTATION_BOUNDARY_REVIEW.md)
> - [`EVAL_ARCHITECTURE_SECOND_REVIEW.md`](./EVAL_ARCHITECTURE_SECOND_REVIEW.md)
>
> 评审目标：审计 Eval 在并发、取消、资源、外部副作用、安全、统计和长期 API 治理方面的边界。

## 1. 结论

第三轮结论：**主领域骨架可保留，但执行平面、评测隔离和平行实验治理仍未达到可实施状态。**

前两轮主要解决“不要重复造 Runtime”；本轮发现的核心问题是“即使复用现有 Runtime，实验之间和 evaluator 之间仍可能互相污染”。

新增阻断项：

1. 没有 experiment coordinator lease 和 fencing，两个 resume 进程可能重复启动同一 Attempt。
2. `max_concurrency` 无法约束 Coding Agent 内部子 Agent、模型请求、浏览器和子进程的资源放大。
3. 取消没有形成 Experiment → Case → Attempt → Product → Agent tree → Tool process 的结构化传播链。
4. fresh workspace 不能隔离数据库、API、对象存储、消息队列等外部副作用，retry 仍可能污染。
5. CommandEvaluator 会写 build cache/node_modules，和“evaluator 只读 workspace”直接冲突。
6. evaluator retry、并发和 metric collision 没有正式状态机与身份规则。
7. Artifact sensitivity 只是元数据；若 CAS 明文落盘，SECRET provenance/diff 仍可能泄漏。
8. repeat 的统计、失败样本、配对比较和 retry 偏差尚未定义。
9. dataset/registry 缺少资源上限和供应链冻结，声明式输入仍可造成 DoS 或实现漂移。
10. ExperimentCompleted 与 report projection、artifact health、预算结算之间的终态边界尚不明确。

## 2. 缺少 Experiment Coordinator Lease

### 2.1 Journal CAS 不足以阻止重复副作用

EventJournal 的 expected-version CAS 只能阻止两个 writer 提交同一个 sequence，不能阻止它们在提交前同时启动外部动作：

```text
Runner A replay 到 AttemptScheduled
Runner B replay 到 AttemptScheduled
A 启动 worker
B 也启动 worker
A 提交 AttemptStarted 成功
B 提交冲突，但 B 的 worker 已经产生副作用
```

因此，“单写者约定”不是进程级所有权保证。

### 2.2 必须增加协调者 lease

```python
class ExperimentCoordinatorLeasePort(Protocol):
    async def acquire(
        self,
        experiment_id: ExperimentId,
        owner_id: str,
        ttl: timedelta,
    ) -> CoordinatorLease: ...

    async def renew(self, lease: CoordinatorLease) -> CoordinatorLease: ...
    async def release(self, lease: CoordinatorLease) -> None: ...
```

Lease 必须包含单调递增 `fencing_token`。所有有副作用的下游请求都携带该 token：

- AttemptExecutor.start；
- Workspace materialization；
- Artifact publication；
- journal fact submission；
- budget reservation；
- cancellation/reclamation。

下游发现旧 fencing token 必须拒绝请求。只做 TTL lock 而没有 fencing，暂停进程恢复后仍可能成为 zombie coordinator。

### 2.3 本地实现也不能省略

本地 runner 可以用进程锁 + durable lease record 实现，但协议必须与未来远程协调一致。`resume` 在获得 coordinator lease 前只能 verify/show，不能启动、取消或修复 execution。

## 3. 并发不是一个 Semaphore

### 3.1 资源放大

`max_concurrency=4` 只限制四个 case/attempt，不限制每个 Coding Agent 内部继续产生：

- 多个子 Agent；
- 多个并发 LLM 请求；
- Terminal/Python 持久进程；
- 浏览器；
- npm/dev server；
- LSP/file watcher；
- background workflows。

因此 case 数量不能代表真实资源消耗。

### 3.2 需要 ResourceProfile 和 Admission

```python
@dataclass(frozen=True, slots=True)
class ResourceProfile:
    cpu_units: int
    memory_bytes: int
    process_slots: int
    agent_slots: int
    model_slots: Mapping[str, int]
    browser_slots: int
    workspace_bytes: int
    artifact_bytes: int
    estimated_cost: Decimal | None


class EvalAdmissionPort(Protocol):
    async def reserve(self, request: AdmissionRequest) -> ResourceReservation: ...
    async def settle(self, reservation: ResourceReservation, usage: ResourceUsage) -> None: ...
    async def release(self, reservation: ResourceReservation) -> None: ...
```

规则：

- 先 durable `AttemptScheduled`，再获得 reservation，最后启动 worker。
- worker 必须继承 reservation 的硬限制。
- AgentControl 的 max_agents/max_depth/cost/token limit 从 Eval policy 显式注入。
- reservation 失败是 admission 状态，不是 task failure。
- coordinator 崩溃后 reservation 必须可 reconcile。
- 同一进程多个 experiment 需要公平排队，不能由先启动实验耗尽全部资源。

第一阶段至少支持进程数、Agent 数、内存、PID、workspace/artifact bytes 和总成本预算。

## 4. 取消必须是结构化所有权树

### 4.1 当前设计只有离散 cancel 方法

当前有 Experiment cancel、AttemptExecutor.cancel、Product facade cancel 等概念，但没有定义传播顺序、deadline 和 acknowledgement。

### 4.2 建议取消树

```text
ExperimentCancellationScope
├── CaseCancellationScope
│   ├── AttemptCancellationScope
│   │   ├── ProductApplicationScope
│   │   │   ├── root Agent
│   │   │   ├── child Agents
│   │   │   ├── background tasks
│   │   │   └── Terminal/Python/tool processes
│   │   ├── workspace lease
│   │   └── artifact writers
│   └── EvaluatorCancellationScope(s)
└── report/projection tasks
```

取消分两阶段：

```text
cooperative cancel
  发出 cancellation intent，等待子 scope acknowledgment。

forced reclaim
  grace period 到期后由 owner 强制终止进程组/cgroup，并记录未确认资源。
```

必须明确：

- fail-fast 是停止新 admission，还是同时取消运行中 case；
- Ctrl+C 第一次是 drain 还是 cancel；
- 第二次是否 force；
- evaluator failure 是否取消同 case 其他 evaluator；
- experiment deadline 和 attempt deadline 谁优先；
- cancel acknowledgment 丢失如何 reconcile；
- `asyncio.CancelledError` 不能作为唯一 durable 取消事实。

推荐默认：fail-fast 停止新 admission，但允许已运行 attempt 进入可配置的 drain；显式 `--cancel-running` 才取消活动执行。

## 5. 外部副作用隔离缺失

### 5.1 Fresh workspace 只隔离文件

Coding Agent 可能修改：

- 数据库；
- 云资源；
- 对象存储；
- Git remote；
- MCP 服务；
- 邮件/消息系统；
- 外部 API；
- 浏览器登录态；
-共享 package/model cache。

第二次 attempt 即使从干净 fixture 开始，也可能观察到第一次 attempt 的外部副作用。

### 5.2 必须声明 SideEffectProfile

```python
@dataclass(frozen=True, slots=True)
class SideEffectProfile:
    network_policy: str
    credential_scope: str
    service_namespaces: Mapping[str, str]
    browser_profile_policy: str
    git_remote_policy: str
    cache_policy: str
    cleanup_policy: str
```

默认 Coding Eval：

- 不提供生产 credentials；
- ask 不自动批准；
- Git remote write 禁止；
- MCP 只允许显式 allowlist；
- 每 attempt 使用独立浏览器 profile；
- 可变外部服务使用 attempt namespace；
- 网络默认 deny，按 task profile 最小开放；
- 无法隔离的外部副作用必须标记 `isolation_grade=partial`；
- partial isolation 的 retry 默认禁止，除非 policy 明确允许。

### 5.3 Retryability 不能只由异常类型决定

Task failure 必须同时满足以下条件才可自动 retry：

- error policy 标记 retryable；
- attempt side-effect receipt 证明可回滚、已清理或 namespace 隔离；
- executor reclaim 达到要求；
- budget 允许；
- retry policy 未耗尽。

否则进入 `retry_blocked_by_side_effects`，等待显式恢复策略。

## 6. Evaluator workspace 模型错误

### 6.1 CommandEvaluator 必然可能写文件

即使使用 argv 且命令本身是测试，常见工具仍会写：

- `node_modules/.cache`；
- `.pytest_cache`；
- coverage 文件；
- build 输出；
- lockfile；
-临时数据库；
-编译缓存。

因此“Evaluator 只能读取被测 workspace”和 CommandEvaluator 不能同时成立。

### 6.2 推荐 immutable snapshot + evaluator overlay

```text
SelectedAttemptSnapshot
  task 完成后生成的只读、内容寻址 workspace tree。

EvaluatorWorkspace
  每个 evaluator attempt 从同一 snapshot 独立物化的 clone/overlay；
  evaluator 可按 capability 写自己的 overlay；
  任何写入都不会影响其他 evaluator 或原始 snapshot。
```

Evaluator capability：

```text
snapshot-read
  只读 WorkspaceView，不创建 overlay。

overlay-write
  获得独立 EvaluatorWorkspace 和受策略命令服务。

networked
  在 overlay-write 基础上按显式网络 policy 执行。
```

报告中的 changed files 必须来自 SelectedAttemptSnapshot 与 fixture 的 diff，不读取 evaluator overlay。

## 7. Evaluator 自身需要 Attempt 模型

主设计为 task 定义 AttemptId，却没有为 evaluator retry 定义稳定身份。建议：

```text
EvaluationId
  case/run + evaluator identity/config 的确定派生 ID。

EvaluatorAttemptId
  EvaluationId + evaluator attempt index 的确定派生 ID。
```

每个 evaluator attempt 有：

-独立 overlay/artifact namespace；
- timeout；
- cancellation；
- execution receipt；
- retry decision；
- terminal status。

metric observations 只来自 selected evaluator attempt。默认 `first_success`，全部失败则 EvaluationResult failed，不能混合多个 attempt 的部分 metrics。

### 7.1 并发与顺序

- evaluator 默认可以并发，因为它们读取同一 immutable snapshot、写独立 overlay。
- declaration order 只用于稳定报告排序，不意味着数据依赖。
- 第一阶段禁止 evaluator 依赖另一个 evaluator 的输出。
- 未来若需要依赖，使用独立 Dataset/Report evaluator DAG ADR，不能偷偷通过共享 artifact 目录传值。

### 7.2 Metric collision

同一 case/run 中 `(evaluator_id, evaluator_config_digest, metric_id)` 必须唯一。

- default evaluator 与 case-specific evaluator 出现相同 identity/config 时，dataset validation 失败；不能运行两次后覆盖。
- 相同 evaluator ID 不同 config 可以共存，但 report display 必须区分实例 ID。
- evaluator 运行时产生未声明、重复或 schema 不匹配 metric，整个 evaluator attempt 失败。

## 8. Artifact sensitivity 不等于安全存储

现有 ArtifactSensitivity 能表达 SECRET，但如果本地 CAS 明文存储，敏感内容仍落盘。Eval 计划保存：

- dirty diff；
-完整配置诊断；
-环境观察；
- stdout/stderr；
- browser state；
- traceback；
- workspace archive。

其中可能包含 token、cookie、PII 和源码秘密。

必须定义：

```text
Redaction before publication
  可安全脱敏的文本先经过 SecretStore-aware redactor，再进入普通 artifact。

Secret artifact store
  无法脱敏但必须保留的内容进入独立加密存储，使用独立 key、ACL 和 retention。

Discard
  无审计必要的秘密不持久化。
```

禁止“先把原文写入普通 CAS，再生成脱敏 representation”，因为原始 blob 已经泄漏到磁盘和 GC 窗口。

Artifact policy 必须在 bytes 写入前决策，而不是仅给 ArtifactRef 标记 sensitivity。

## 9. Dataset 与 Registry 的资源和供应链边界

### 9.1 Dataset DoS

不可信 dataset 必须有解析上限：

-配置文件 bytes；
- case 数量；
- evaluator 数量；
-嵌套深度；
-字符串长度；
- metadata entries/bytes；
- fixture 文件数量、单文件和总 bytes；
- symlink、hardlink、device/FIFO/socket；
-压缩包展开比例；
- YAML alias 数量和递归。

超限在 materialization 前失败，不能先复制/解压再检查。

### 9.2 Registry 冻结

实验开始时生成不可变 `ExecutionManifest`：

```text
task/evaluator/codec/policy stable ID
declared version
implementation source/package digest
distribution name/version
entry-point origin
capability requirements
schema decoder IDs
```

同一实验 resume 必须使用相同 manifest，不能因升级安装包而解析到新实现。

插件注册要求：

-启动期显式注册并冻结；
-拒绝 dataset 指定任意 import path；
- stable ID 冲突 fail-fast；
-实现版本与 distribution version 不得互相替代；
-可选签名/allowlist 属于 host policy；
-历史 show/replay 不加载 executable plugin。

## 10. 缓存必须成为实验输入

依赖和模型缓存会显著改变 duration、网络行为甚至结果。需要明确：

```text
cache_mode:
  cold       不挂载共享 cache
  readonly   挂载有 digest 的只读 cache snapshot
  private    attempt 私有可写 cache
  shared     显式非隔离模式，默认不可比较
```

禁止多个 attempt 共享可写 package/build cache作为默认优化。

provenance 记录 cache mode、snapshot digest 和命中统计。比较器默认拒绝 cold 与 shared 的性能指标直接比较。

## 11. Budget 与 Cost 必须在 admission 前闭合

Mote 已有模型 usage/cost 和 inference budget primitives，但 Eval 仍需实验级 policy：

```text
max_total_cost
max_total_tokens
max_case_cost
max_attempt_cost
max_wall_time
max_active_attempts
max_retry_cost
```

要求：

-启动 attempt 前预留 budget；
-完成后 settlement；
- worker 丢失后进入 pending reconciliation；
-预算不足是 skipped/admission rejected，不是假 task failure；
-实际 provider fallback 成本归属原 attempt；
-取消后仍需结算已发生 usage；
- report 区分 estimated、reserved、settled 和 unknown cost。

Product headless receipt 必须提供公开 cost/usage projection，Eval 不读取内部 CostTracker。

## 12. Repeat 的统计语义不足

### 12.1 Repeat、Retry、Best-of-N 必须区分

```text
repeat
  独立样本，进入统计聚合。

retry
  同一样本因运行失败重新尝试，只选择一个 terminal attempt。

best-of-N
  多个成功候选中选择最好结果；这是独立实验策略，第一阶段禁止。
```

### 12.2 失败样本不能静默排除

报告必须声明每个 metric 的 denominator policy：

```text
all_runs
successful_task_runs
observed_metric_runs
```

默认 assertion rate 使用 all_runs；task failure 不能从分母消失。Scalar 的 missing/failure 不能自动当 0，也不能自动排除，必须由 MetricDefinition/ReportPolicy 明确。

### 12.3 比较语义

- 相同 case/repeat seed 的两个实验优先做 paired comparison。
- seed 或 case digest 不匹配时只能 unpaired。
- report 显示 sample count、missing count、failure count 和 interval method。
- 小样本默认不输出“显著提升”结论。
- aggregation、置信区间和比较方法必须有版本化 policy ID。
- retry 次数和成功率单独报告，不能只展示 selected attempt 指标。

## 13. Observable facts 与 Telemetry 必须分开

实验 journal 只保存影响恢复、结果和审计的事实。高频进度、日志、token streaming 和 heartbeat 属于 telemetry。

```text
Journal
  admission、ownership、phase transition、receipt、metric、terminal、repair。

Telemetry
  progress、debug log、resource samples、token stream、queue wait samples。
```

Telemetry 丢失不能改变 reducer 结果。需要进入报告的资源统计必须由 terminal receipt 汇总成 durable fact，不能回扫日志推导。

## 14. Storage layout 与多租户路径

`.mote/eval/runs/<experiment-id>` 只是展示，不足以成为安全布局契约。

需要 `EvalStorageLayout`：

- root 由 Product RuntimePaths/host 配置注入；
-所有 ID 作为路径段前先验证，禁止直接使用 display name；
- experiment owner/tenant/project scope 显式；
-目录权限默认 0700，文件默认 0600；
- journal、projection、workspace、secret artifacts 分开根目录；
- symlink 不可改变 scope；
- quota 在写入前检查；
-删除使用 tombstone + GC/reconcile，不直接递归删除未知路径；
- legal hold/pinned artifact 不随 experiment 删除；
- Windows/POSIX path 与 case-folding 差异有测试。

CLI 不能通过用户提供 experiment ID 拼接任意路径后读取。

## 15. Experiment 终态需要拆分

`ExperimentCompleted` 不应同时表示 execution、projection 和 publication 全部成功。

建议正交状态：

```text
execution_status:
  open | completed | cancelled | failed

projection_status:
  pending | current | failed | stale

publication_status:
  not_requested | pending | completed | failed

budget_status:
  open | settled | reconciliation_required

integrity_status:
  verified | blocked | repaired | unknown
```

Experiment execution terminal 的条件：所有 case terminal 或有明确取消/aborted 决策，且没有未对账 execution lease。

Report projection 失败不能回滚 Experiment execution terminal；修复 projection 也不应新增第二个 ExperimentCompleted。

## 16. Public API 与 Schema 治理

十年 API 需要明确稳定等级：

```text
Public stable
  mote.eval 导出的 Dataset source schema、runner request、report view。

Durable stable
  event payload、materialized dataset、receipt、metric、provenance schema。

Internal
  reducer implementation、local executor、projection builders。
```

要求：

-公开类型进入兼容性清单；
- durable schema 每个都有 owner 和 migration policy；
-删除字段只能通过新 schema version；
- unknown field 行为显式；
- enum 扩展兼容性逐字段定义；
- Python API version 与 durable schema version 分离；
- `eval/__init__.py` 不一次性导出所有内部 domain 类；
-不承诺内部文件路径为 API。

## 17. 修订后的执行关系

```text
ExperimentRunner
  acquire coordinator lease + fencing token
  → materialize/freeze dataset and execution manifest
  → reserve experiment budgets
  → CaseMachine(s)
      → reserve resources
      → AttemptMachine(s)
          → materialize private workspace
          → Product headless application
          → task teardown
          → executor reclamation
          → immutable selected snapshot
      → select attempt
      → EvaluatorAttempt(s)
          → private overlay
          → policy-bound capabilities
          → metric observations
      → finalize immutable CaseResultFact
  → settle leases/budgets
  → terminal experiment execution fact
  → rebuild report projection
```

## 18. 新增实现前门槛

在前两轮门槛基础上增加：

1. 同一 experiment 同时只能有一个有效 coordinator fencing token。
2. 两个并发 resume 进程不能重复启动同一 Attempt。
3. admission 同时约束 case、Agent、进程、内存、workspace、artifact 和成本预算。
4. cancellation 有结构化传播、grace period、force reclaim 和 durable acknowledgment。
5. retry 前必须验证外部副作用隔离或明确阻止自动 retry。
6. selected attempt workspace 在 evaluator 前冻结为 immutable snapshot。
7. 每个写型 evaluator 使用独立 overlay，顺序变化不改变其他 evaluator 结果。
8. evaluator retry 具有独立 EvaluatorAttemptId、artifact namespace 和 selection policy。
9. metric identity collision 在 dataset materialization 阶段失败。
10. SECRET bytes 不先写普通明文 CAS；redaction/encryption decision 在 publication 前完成。
11. dataset parser/materializer 有完整资源和压缩展开上限。
12. ExecutionManifest 冻结所有可执行实现和 capability requirements。
13. cache mode 和 cache digest 进入 provenance 与比较兼容性判断。
14. budget reservation/settlement/reconcile 覆盖成功、取消、超时和 worker lost。
15. repeat 报告显式展示 denominator、missing、failure、retry 和 sample count。
16. telemetry 丢失不影响 journal reducer；报告资源统计来自 durable receipt。
17. storage layout 具备 tenant scope、权限、quota、tombstone 和路径穿越测试。
18. experiment execution、projection、publication、budget 和 integrity 状态保持正交。
19. public API、durable schema 和 internal implementation 有不同兼容承诺。
20. coordinator crash、worker crash、双 runner、磁盘满、budget settlement 失败均有恢复测试。

## 19. 最终判断

主设计距离“可实现”还差的不是更多 evaluator，而是一个真正闭合的实验执行控制面：

- coordinator fencing 防止 split-brain；
-多维 admission 防止资源放大；
-结构化 cancellation 和外部副作用隔离保证 retry 正确；
- immutable snapshot + evaluator overlay 保证评分不互相污染；
-预算、统计和存储治理保证规模化后结果仍可信。

建议下一步先把三轮评审合并回主设计，形成一份无冲突的 canonical specification，再做第四轮“逐状态、逐端口、逐 durable schema”的一致性审计。在完成合并前，不建议继续扩充功能范围或开始编码。
