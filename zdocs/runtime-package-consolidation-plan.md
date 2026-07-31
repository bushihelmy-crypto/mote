# Runtime 二阶段分包治理计划

本文是 P1-P6 拆包后的二阶段计划。P1-P6 已经解决主要 ownership 问题，本计划不再以“减少目录数”为驱动，而是处理剩余的边界噪声、路径依赖倒置、错误归属纠偏和低风险 namespace 收束。

## 1. 结论

当前 `runtime/` 已经基本是 runtime capabilities 的集合，不再是一个混杂大包。二阶段不应把清晰 capability 为了数字合并掉。

二阶段目标：

1. 给每个当前 `runtime` 顶层入口确定唯一 disposition：`retain` / `merge` / `split` / `delete`。
2. 消除 runtime capability 主动 discovery 产品路径的模式。
3. 修正 workspace、completion、maintenance、reconciliation 等剩余错误归属。
4. 只合并真正同质的 substrate：telemetry、control、persistence primitives。
5. 原子更新 import 并删除旧路径，不为旧路径保留 compatibility re-export、alias、forwarding package。

非目标：

- 不追求把顶层包压到固定数量。
- 不建立 `common`、`utils`、`helpers`、`misc`、`shared` 这类泛化包。
- 不把 `runtime.events` 归入 telemetry；它是运行时事件投递平面。
- 不把整个 `workspace`、`durable`、`ledger` 机械并入 persistence。
- 不为了路径整齐给单文件强行套空壳子包。

## 2. 执行原则

### 2.1 数量是结果指标

顶层包数量只作为观测指标。合理终态预计仍在高 20 个左右；如果某个 capability 边界清晰，保留顶层优先于合并凑数。

### 2.2 原子迁移

每个原子 slice 内必须一次完成：

1. 移动实现。
2. 更新全仓 import。
3. 删除旧入口。
4. 更新架构测试或显式检查。

禁止：

- 为旧路径保留 compatibility re-export。
- 为旧路径保留 `__init__.py` 兼容转发。
- 保留旧包空目录作为兼容入口。
- “下一阶段再删”的 alias。

允许：

- 经过架构测试约束的 canonical package API。
- canonical API 必须指向新 owner，不得转发旧路径。
- `__init__.py` 只能暴露稳定公共类型，不得扩大 public API 来掩盖迁移。

### 2.3 先语义，后目录

目录移动只有在依赖方向、owner 和 public API 同时变清楚时才执行。不能用移动路径替代领域拆分。

## 3. 当前顶层 Disposition

以下清单以当前工作树为准，覆盖 `runtime/` 下所有顶层目录和顶层 `.py` 文件。进入实施前 C0 必须重新生成清单；如新增入口，必须补入本表后才能继续。

### 3.1 顶层目录

| 当前入口 | 处置 | 目标 | 说明 |
| --- | --- | --- | --- |
| `runtime.agent` | retain | `runtime.agent` | 单 Agent runtime、Role、component wiring。 |
| `runtime.artifacts` | retain | `runtime.artifacts` | 通用 artifact/CAS/store/publication/GC。 |
| `runtime.code_map` | retain | `runtime.code_map` | CodeMap 索引和执行机制；路径策略由 product 注入。 |
| `runtime.completion` | split | `runtime.agent.completion` | Agent run completion policy，不属于 generic control。 |
| `runtime.config` | retain | `runtime.config` | 配置读取和 layering；不拥有所有 capability 路径语义。 |
| `runtime.context` | retain | `runtime.context` | history、compaction、turn bus、runtime prompt fragment 消费。 |
| `runtime.disk` | merge | `runtime.persistence` | 纯 disk/atomic IO primitive，可收束为 persistence primitive。 |
| `runtime.durable` | retain | `runtime.durable` | 含 ThinkJournal、Temporal 等执行语义；语义拆分前不进 persistence。 |
| `runtime.errors` | retain | `runtime.errors` | runtime 错误分类。 |
| `runtime.events` | retain | `runtime.events` | EventFabric、subscription、journal、backpressure；不是 observability。 |
| `runtime.fileops` | retain | `runtime.fileops` | 工作区文件读写、mutation、review、FileOps roots/pins。 |
| `runtime.hook` | retain | `runtime.hook` | hook 运行时聚合、adapter、执行逻辑。 |
| `runtime.interactive` | retain | `runtime.interactive` | browser/terminal/kernel/canvas/device/video 等交互后端。 |
| `runtime.ledger` | retain | `runtime.ledger` | run/tool/think/timer 运行语义；语义拆分前不进 persistence。 |
| `runtime.logging` | merge | `runtime.telemetry.logging` | logging substrate；human-input console adapter 先移出或隔离到 product。 |
| `runtime.lsp` | retain | `runtime.lsp` | 通用 runtime capability。 |
| `runtime.media` | retain | `runtime.media` | runtime media 处理能力。 |
| `runtime.models` | retain | `runtime.models` | 模型客户端、路由、cost、rate-limit、auth、模型 adapter。 |
| `runtime.observability` | merge | `runtime.telemetry.observability` | tracing/backend integrations。 |
| `runtime.output` | retain | `runtime.output` | runtime output commit/event wrapper；纯语义在 kernel。 |
| `runtime.projections` | retain | `runtime.projections` | runtime projection substrate。 |
| `runtime.prompt` | retain | `runtime.prompt` | prompt admission、sanitization、runtime prompt policy 执行。 |
| `runtime.resilience` | retain | `runtime.resilience` | 通用 admission/failover/retry/classification/breaker。 |
| `runtime.resources` | retain | `runtime.resources` | runtime resource accounting/control。 |
| `runtime.sandbox` | retain | `runtime.sandbox` | sandbox runtime、network/seccomp/resource isolation。 |
| `runtime.scheduling` | merge | `runtime.control.scheduling` | runtime periodic scheduling primitive。 |
| `runtime.secrets` | retain | `runtime.secrets` | vault、cipher、secret refs、TOTP。 |
| `runtime.service_gateway` | retain | `runtime.service_gateway` | externally hosted tool/service gateway。 |
| `runtime.session` | retain | `runtime.session` | JSONL session log、replay、checkpoint、handoff。 |
| `runtime.tools` | retain | `runtime.tools` | 工具执行框架、permission、result、MCP adapter。 |
| `runtime.vcs` | retain | `runtime.vcs` | runtime VCS capability。 |
| `runtime.watching` | retain | `runtime.watching` | file/watch runtime capability。 |
| `runtime.workspace` | split | `runtime.session.workspace` + `runtime.persistence` | session footprint layout 和生命周期归 `runtime.session.workspace`；无领域 IO primitive 进 `runtime.persistence`。 |

### 3.2 顶层文件

| 当前入口 | 处置 | 目标 | 说明 |
| --- | --- | --- | --- |
| `runtime/__init__.py` | retain | `runtime/__init__.py` | 仅 package marker；不得为旧路径提供兼容转发。 |
| `runtime/engine.py` | retain | `runtime.engine` | runtime engine 入口，保持顶层。 |
| `runtime/leases.py` | merge | `runtime.control.leases` | lease primitive，和 lifecycle/scheduling 同质。 |
| `runtime/lifecycle.py` | merge | `runtime.control.lifecycle` | lifecycle stack/resource close ordering。 |
| `runtime/maintenance.py` | split | owning domains | repo scan、workspace cleanup 等具体协调逻辑按 owner 回归，不进 control。 |
| `runtime/paths.py` | split | product composition / domain layout / injected `Path` | 拆除 `.mote` discovery 与默认路径耦合。 |
| `runtime/process.py` | retain | `runtime.process` | runtime process primitive；本计划不移动。 |
| `runtime/reconciliation.py` | split | `runtime.resilience` / `runtime.artifacts` / `runtime.durable` | artifact/durable failure classification 按语义归属，不进 control。 |
| `runtime/reporting.py` | retain | `runtime.reporting` | runtime reporting facade；本计划不并入 telemetry。 |
| `runtime/run_context.py` | retain | `runtime.run_context` | runtime run context primitive。 |
| `runtime/services.py` | retain | `runtime.services` | service wiring primitive；本计划不移动。 |

## 4. Phase C0：基线校准

C0 不移动代码，只建立实施基线。没有 C0 基线，不批准后续 phase。

### 4.1 顶层入口基线

生成当前顶层目录和文件：

```bash
find runtime -mindepth 1 -maxdepth 1 -type d ! -name '__pycache__' -printf '%f\n' | sort
find runtime -mindepth 1 -maxdepth 1 -type f -name '*.py' -printf '%f\n' | sort
```

验收：

- 清单与本文第 3 节逐项匹配。
- 每个入口恰好一个 disposition。
- `runtime.session`、`runtime.vcs`、`runtime.watching` 不得遗漏。

### 4.2 依赖指标基线

依赖图指标来源：`ztest/architecture/runtime_dependency_metrics.py`。

统计口径：

- 节点：runtime production modules，不含 `ztest`。
- 边：production import edge。
- SCC：非平凡 SCC，即 size > 1。
- fan-out：按 module 和 package 两个口径分别记录。

必须记录：

| 指标 | 迁移前基线 | 迁移后目标 |
| --- | --- | --- |
| runtime 顶层入口数 | C0 记录 | 不作为硬性下降目标 |
| 非平凡 SCC 数量 | C0 记录 | 不增加 |
| 非平凡 SCC 总节点数 | C0 记录 | 不增加；C2 语义拆分要求下降 |
| 最大非平凡 SCC size | C0 记录 | 不增加；C2 语义拆分要求下降 |
| 非法依赖边数量 | C0 记录 | 由架构测试归零 |
| top fan-out modules | C0 记录 | 不因合并产生新 hub |
| top fan-out packages | C0 记录 | 不因合并产生新 hub |

### 4.3 架构规则基线

`runtime_dependency_metrics.py` 只负责依赖图和 SCC/fan-out 指标，不能单独证明所有边界规则。C0 必须同时确认或补齐 `ztest/architecture` 规则：

- runtime production code 不 import product。
- runtime persistence 不 import session、tools、artifacts、fileops、orchestration 等领域包。
- runtime telemetry 不承载 event fabric。
- runtime control 只包含 lifecycle、leases、scheduling。
- 旧路径 compatibility re-export、alias、forwarding package 为零。
- `runtime.persistence.paths` 只允许路径安全、规范化和 atomic path primitive，禁止默认根目录、discovery 和领域 layout。

## 5. Phase C1：路径依赖倒置

C1 优先级最高。它比 namespace 合并更有架构价值，因为它直接切断 runtime capability 对产品路径策略的反向依赖。

### 5.1 目标模型

- product composition root 负责发现 `.mote`、用户目录、项目目录和产品默认值。
- runtime config 负责读取配置和 layering，不拥有所有路径语义。
- runtime capability 构造器接收明确 `Path` 或领域 config。
- session layout、browser profile、vault path、sandbox CA path、CodeMap store path 分别归对应领域或由 product 注入。

### 5.2 原子 Slices

C1 不是一个大爆炸迁移，而是一组原子 slice。每个 slice 必须一次完成自己的 owner 迁移、import 更新和测试；只有 C1e 删除 `runtime.paths`。

| Slice | 范围 | 完成条件 |
| --- | --- | --- |
| C1a | 产品 `.mote` discovery | `product.agents`、`product.skills`、`product.code_map` 不再 import `runtime.paths` 的 discovery helper。 |
| C1b | session/workspace defaults | session workspace 从构造参数接收 root；默认值由 product composition 或 config wiring 注入。 |
| C1c | secrets/oauth defaults | vault、oauth storage 从构造参数接收路径；不直接读取 `CONFIG_ROOT`。 |
| C1d | browser/sandbox defaults | browser profile、sandbox CA 路径由构造参数或 capability config 注入。 |
| C1e | 删除旧入口 | 删除 `runtime.paths`，旧 import 检查归零。 |

### 5.3 `runtime.paths` 拆分

| 当前职责 | 目标 owner |
| --- | --- |
| `.mote` discovery | product composition root 或对应 product domain |
| agent/skills/code_map/product 默认路径 | `product.agents` / `product.skills` / `product.code_map` |
| session workspace layout | `runtime.session.workspace`，root 由构造参数注入 |
| browser profile path | `runtime.interactive` 构造参数 |
| vault path | `runtime.secrets` 构造参数 |
| sandbox CA path | `runtime.sandbox` 构造参数 |
| package data path | 明确命名的 config/package data helper |

### 5.4 CodeMap 硬约束

`runtime.code_map` 只能接收 `store_path`、语言启用策略和索引参数，禁止：

- import `runtime.paths`。
- 读取 `CONFIG_ROOT`。
- 执行 `.mote` discovery。
- 自行决定用户/项目层级策略。

### 5.5 验收

检查必须可判定。示例：

```bash
if rg -n 'from mote\.runtime\.paths|import mote\.runtime\.paths|CONFIG_ROOT|mote_project_|mote_layered_' runtime/code_map --glob '*.py'; then
  exit 1
fi
```

C1a 完成后，CodeMap/product discovery 的旧 import 必须归零。C1b-C1d 各自完成后，其 slice 范围内旧 import 必须归零。C1e 完成后，全仓 `runtime.paths` import 必须归零并删除旧模块。

## 6. Phase C2：领域纠偏

C2 处理上一版计划中被错误合并的模块。原则是先拆语义，再决定路径。

### 6.1 `runtime.workspace`

当前 `WorkspaceStore` 同时承载存储 primitive 和 session、rollout、tool result、task output、ledger 等领域布局。不能整体迁入 `runtime.persistence`。

目标：

- 保留一个领域化聚合：`runtime.session.workspace`。
- 引入 `SessionWorkspace`、`SessionLayout`、`SessionSpace`，作为同一 session footprint 的唯一布局和生命周期 owner。
- rollout、tool results、task outputs、ledger 等仍位于同一 session root 下，保证删除 session 即删除全部 footprint。
- tools、ledger、orchestration.tasks 接收窄接口或具体 `Path`，不各自定义 session 目录布局。
- 无领域语义的 atomic storage、path-safe IO、directory primitive 进入 `runtime.persistence`。
- `runtime.workspace` 顶层入口删除，不保留 facade、alias 或兼容转发。

验收：

- `runtime.persistence` 不 import `runtime.session`、`runtime.tools`、`runtime.artifacts`、`runtime.fileops`、`orchestration`。
- `runtime.session.workspace` 是 session layout 和 cleanup lifecycle 的唯一 owner。
- tools、ledger、tasks 不拼接 session layout 常量，只消费 `SessionSpace` / narrow port / injected `Path`。
- cleanup 不需要知道所有消费者；新增 session-scoped artifact kind 只扩展 session workspace layout。

### 6.2 `runtime.completion`

`completion` 是 Agent run completion policy，当前依赖 hook 运行逻辑。它不属于 generic control。

目标：

- Agent completion policy 进入 `runtime.agent.completion`。
- 仅跨边界 DTO/Protocol 放 `contracts`。
- hook aggregation 和 runtime 快路径留在 `runtime.hook`。

验收：

```bash
if rg -n 'mote\.runtime\.completion' contracts kernel orchestration product ztest runtime --glob '*.py'; then
  exit 1
fi
```

### 6.3 `runtime.maintenance`

`maintenance` 不是 generic maintenance loop，而是 repo scan、workspace cleanup 等具体协调器。

目标：

- repo scan 归 CodeMap/VCS owning domain。
- workspace cleanup 归 `runtime.session.workspace`。
- agent 触发 adapter 留在 `runtime.agent` component。
- 不进入 `runtime.control`。

### 6.4 `runtime.reconciliation`

`reconciliation.py` 当前更接近 artifact/durable failure classification，不是 reconciliation loop。

目标：

- 通用 failure classifier 进入 `runtime.resilience`。
- artifact-specific recovery 归 `runtime.artifacts`。
- durable-specific recovery 归 `runtime.durable`。
- 不进入 `runtime.control`。

### 6.5 `runtime.durable` 与 `runtime.ledger`

二阶段保留顶层。只有在完成语义 ADR 后，才允许拆出纯 persistence primitive。

ADR 必须回答：

- 哪些 API 是 append/write/read primitive。
- 哪些 API 是 think/tool/timer/session 语义。
- 拆分后是否减少非法边、fan-out 或 SCC。

## 7. Phase C3：低风险 Namespace 收束

C3 只合并同质 substrate，不做跨领域聚合。

### 7.1 Telemetry

目标：

```text
runtime.telemetry/
  logging/
  observability/
```

保留：

```text
runtime.events/
```

要求：

- `runtime.events` 继续作为事件投递平面，承载 EventFabric、subscription、journal、backpressure。
- `contracts/events` 继续承载事件数据契约。
- `kernel.telemetry` 继续是 kernel 注入能力，不并入 runtime telemetry。
- logging 中的 human-input console adapter 先移到 product/CLI adapter，再合并 telemetry substrate。

验收：

```bash
if rg -n 'mote\.runtime\.(logging|observability)' contracts kernel runtime orchestration product ztest --glob '*.py'; then
  exit 1
fi

if rg -n 'mote\.runtime\.telemetry\.events' contracts kernel runtime orchestration product ztest --glob '*.py'; then
  exit 1
fi
```

### 7.2 Control

目标：

```text
runtime.control/
  lifecycle.py
  leases.py
  scheduling/
```

只允许放：

- lifecycle stack/resource close ordering。
- lease coordinator/lease handles。
- runtime periodic scheduling primitive。

明确禁止：

- `completion`。
- `maintenance`。
- `reconciliation`。
- orchestration scheduler。
- Agent-specific control adapter。

验收：

```bash
if rg -n 'mote\.runtime\.(lifecycle|leases|scheduling)' contracts kernel runtime orchestration product ztest --glob '*.py'; then
  exit 1
fi
```

### 7.3 Persistence Primitive

目标：

```text
runtime.persistence/
  __init__.py
  atomic.py
  async_io.py
  journal_writer.py
  paths.py
```

允许来源：

- `runtime.disk` 中无领域语义的 disk/atomic IO primitive。

禁止来源：

- 整个 `runtime.workspace`。
- 整个 `runtime.durable`。
- 整个 `runtime.ledger`。
- session/tool/task/artifact layout。

`runtime.persistence.paths` 只允许路径安全、规范化、atomic write 相关 primitive，禁止：

- 默认根目录。
- `.mote` discovery。
- session/workspace layout。
- browser/sandbox/secrets 等 capability 路径策略。

验收：

```bash
if rg -n 'mote\.runtime\.disk' contracts kernel runtime orchestration product ztest --glob '*.py'; then
  exit 1
fi

if rg -n 'from mote\.runtime\.persistence import .*Workspace|mote\.runtime\.persistence\.(workspace|durable|ledger)' runtime product orchestration ztest --glob '*.py'; then
  exit 1
fi
```

## 8. Phase C4：大包内部治理

C4 不以新增子包为目标，只在模块数量、API 面或依赖方向已经证明需要时调整内部结构。

优先对象：

| 包 | 治理方向 |
| --- | --- |
| `runtime.tools` | 区分 executor、capability injection、permission、result、MCP adapter；避免 tool runtime 反向依赖 product toolsets。 |
| `runtime.fileops` | FileOps-scoped artifact support 改名，避免和 `runtime.artifacts` 通用 artifact store 混淆。 |
| `runtime.models` | 保持 clients/routing/failover/cost/ratelimit/auth 边界；不把 service_gateway 并入 models。 |
| `runtime.agent` | component adapter 集中治理；Role 不新增硬编码子系统状态。 |
| `runtime.context` | 保持 history/compaction/turn bus；不重新吸收 Skills/CodeMap 产品策略。 |

禁止：

- 为单文件创建空壳目录。
- 通过 `__init__.py` 扩大 public API。
- 为了减少 import 长度增加 compatibility re-export。

## 9. Phase C5：原子迁移与验证

每个实施 PR 或变更集必须同时包含：

1. 代码移动。
2. import 更新。
3. 删除旧路径。
4. 架构测试或可判定 shell 检查。
5. 相关 ztest。

### 9.1 Import 清理规则

所有 `rg` 检查必须是 pass/fail，不允许只打印结果。

格式：

```bash
if rg -n 'PATTERN' contracts kernel runtime orchestration product ztest --glob '*.py'; then
  exit 1
fi
```

### 9.2 架构测试

优先把规则写入现有 `ztest/architecture`，避免平行 AST 扫描器。

必须覆盖：

- production import。
- `TYPE_CHECKING` import。
- runtime 向 product/orchestration 的反向 import。
- 禁止 `common` / `utils` / `helpers` 新包。
- 禁止旧路径 import。
- 禁止旧路径 compatibility re-export。

### 9.3 测试范围

按变更范围选择，不写不存在的测试路径。

最低要求：

| Phase | 必跑 |
| --- | --- |
| C1 paths | `ztest/architecture`，以及被改 capability 的直接 ztest |
| C2 workspace | `ztest/architecture ztest/session ztest/executor ztest/tasks ztest/common/workspace ztest/common/ledger` |
| C2 completion | `ztest/architecture ztest/roles ztest/runtime ztest/hook` |
| C3 telemetry | `ztest/architecture ztest/events ztest/observability ztest/roles` |
| C3 control | `ztest/architecture`，以及 lifecycle/lease/scheduling 直接测试 |
| C3 persistence | `ztest/architecture`，以及 disk primitive 直接测试和调用方测试 |

最终验收必须包含：

```bash
python -B -m pytest ztest/architecture -q --tb=short
```

如果某路径在实施分支中不存在，必须在变更说明中解释对应测试被迁移到哪个现有路径；不能把不存在路径写成验收命令。

## 10. 预期终态

终态不是固定包数量，而是满足以下结构：

### 10.1 保留顶层 capability

```text
runtime.agent
runtime.artifacts
runtime.code_map
runtime.config
runtime.context
runtime.durable
runtime.errors
runtime.events
runtime.fileops
runtime.hook
runtime.interactive
runtime.ledger
runtime.lsp
runtime.media
runtime.models
runtime.output
runtime.projections
runtime.prompt
runtime.resilience
runtime.resources
runtime.sandbox
runtime.secrets
runtime.service_gateway
runtime.session
runtime.tools
runtime.vcs
runtime.watching
```

### 10.2 新增或收束 substrate

```text
runtime.control
runtime.persistence
runtime.telemetry
```

### 10.3 删除或拆除的旧入口

```text
runtime.completion        -> runtime.agent.completion
runtime.disk              -> runtime.persistence
runtime.logging           -> runtime.telemetry.logging
runtime.observability     -> runtime.telemetry.observability
runtime.scheduling        -> runtime.control.scheduling
runtime.lifecycle         -> runtime.control.lifecycle
runtime.leases            -> runtime.control.leases
runtime.paths             -> split by owner/injection
runtime.maintenance       -> split by owner
runtime.reconciliation    -> split by owner
```

`runtime.workspace` 拆分完成后删除顶层入口；session-scoped layout 聚合迁入 `runtime.session.workspace`，不得以 facade、alias 或旧路径 compatibility re-export 形式保留。

## 11. 最终验收标准

批准执行前：

- 第 3 节 disposition 覆盖当前所有顶层入口。
- C0 记录依赖指标基线。
- 所有未决路径都有唯一 owner 和 phase。
- 不存在旧路径 compatibility re-export、alias、forwarding package 方案。

执行完成后：

- 旧 import 检查全部归零。
- 旧目录和旧文件删除。
- `runtime.events` 仍为顶层事件投递平面。
- `runtime.control` 只包含 lifecycle、leases、scheduling。
- `runtime.persistence` 只包含无领域语义的 disk/storage/path primitive。
- `runtime.session.workspace` 是同一 session footprint 的唯一布局和 lifecycle owner。
- `runtime.telemetry` 不承载 event fabric。
- runtime capability 不主动 discovery 产品 `.mote` 路径。
- 非法依赖边归零。
- 非平凡 SCC 数量、总节点数、最大 size 不增加；C2 语义拆分涉及的 SCC 指标下降。
- 未新增 `common`、`utils`、`helpers`、compat、alias、旧路径 compatibility re-export。
