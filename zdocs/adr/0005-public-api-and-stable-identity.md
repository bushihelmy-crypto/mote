# ADR-0005：Public API、稳定身份与 Breaking Release

- 状态：Accepted；已与产品负责人确认仓库外没有 Python import 调用方
- 日期：2026-07-29
- 决策 owner：Product/API 与 Orchestration 联合评审

## 背景

目录迁移会同时影响 import API 和派生 identity。当前 `AgentCatalog.version` 使用 `__module__`、`__qualname__` 和 source digest；即使字段未直接持久化，搬包也会改变 catalog version 和外部可观察行为。

最终源码不保留旧 path，与发布期间是否提供兼容窗口是两个问题，必须分别决策。

## 决策

### 稳定身份清单

Phase 0 建立机器可审计清单，逐项记录 owner、当前算法、外部观察者、迁移策略和测试：

- 持久化 JSON 字段、schema version、Pydantic discriminator。
- module/qualname/source 派生 version/hash。
- Agent/Tool/Workflow canonical name 和 registry key。
- error/event tag、telemetry identity、cache key。
- Residency/Role loader、task result、workflow snapshot、Cron store identity。

移动 Python 模块不得无意改变清单中的 identity。需要改变时使用显式 schema/version migration，并作为 breaking change 审批。

### Agent Catalog

`AgentCatalog` 属于 Product agent declaration/discovery，而非 Orchestration fleet identity，迁入 `product.agents`。Catalog version 改为稳定声明：

```text
agent_name + explicit definition_version + canonical definition digest
```

canonical digest 的输入必须排除 Python module path。Markdown Agent 可继续使用内容 digest；Python Agent 需要显式 `definition_version`，source digest 只能作为开发期变化检测信号，不能作为跨构建稳定身份，除非构建可重复性经过证明。

### API 分类

以下路径已确认为 `repository internal`：

```text
mote.orchestration.environment
mote.orchestration.tasks
mote.orchestration.tasks.bggraph
```

内置 `run_graph`、`resume_tasks`、`get_node_state` 等 Tool 是仓内调用方，必须在同一 Slice 原子更新，不构成保留旧 Python path 的理由。

该分类不意味着所有行为都可任意 breaking。以下仍是外部可观察稳定契约：

- 模型看到的 `RunGraph/run_graph` 工具名、参数 JSON Schema 和核心行为。
- Graph output contract identity。
- 已落盘的 workflow/task/run state、event/error tags 和 result pointer。
- telemetry/activity 中用于 replay/projector 的稳定字段。

因此允许删除 `BgGraph` Python class/path，但默认保持 `run_graph` Tool Schema 和行为；需要改变后者时必须单独评审并迁移 durable data。

### 发布策略

本次采用 repository-internal 路径：同一 Slice 更新全部仓内调用方并删除旧路径，不创建 compatibility package。

未来若某路径被明确发布为 committed external，则采用：

1. 指定 breaking release 和最终删除版本。
2. 发布迁移表、release note 和 deprecation window。
3. 兼容包只允许存在于明确的迁移发布版本，不能扩展功能。
4. 新实现只有一个；兼容包仅转发并有旧/新 consumer contract test。
5. 删除版本移除兼容包并增加旧 import 失败门禁。

治理“完成态”始终不包含兼容包；但实施计划允许在已批准的发布窗口暂时存在转发。这是对最终架构纪律的时间边界，不是永久例外。

### 迁移中断

- 数据迁移必须带 schema version、幂等 marker 和重试测试。
- 进程在迁移任一点中断后，重跑只能完成同一 identity 转换，不能生成第二身份。
- 旧 reader/new writer 或 dual-write 只有在单独 ADR 证明必要时允许；默认采用停写、迁移、切换、删除旧路径。

## 拒绝方案

- 用旧 module re-export 永久维持 identity。
- 默认所有 `__all__` 都是外部 API，或默认都不是。
- 只扫描 JSON 而忽略 hash/cache/registry key。
- 发布窗口没有删除版本。

## 验收

- 稳定身份清单覆盖所有已知类别并有自动化断言。
- Agent 类型只因 module move 不改变 catalog identity。
- 三个旧路径已经书面分类为 repository-internal，并由 AST 门禁保证同 Slice 删除。
- `run_graph` Tool contract 与 Python import path 被分别测试，不能混为一类兼容性。
- 迁移中断和重复执行测试通过。
