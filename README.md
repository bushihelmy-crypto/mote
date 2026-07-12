# AgentFrame

`metagpt/`（`metagpt.*` 包）是一个**组合式、事件驱动、分层解耦**的 agent 运行框架。它把一个 agent 的运行拆成「想（think）/ 做（act）」对偶的 ReAct 循环，外接多 agent 运行时、会话持久化、统一 LLM 路由与权限沙箱，全部架在一个零反向依赖的 `common` 基础层之上。


## 核心特性

- **组合优于继承的 Role**：`Role` 是纯编排器——静态配置 `RoleSchema` + 可序列化运行态 `RoleState` + 惰性装配的组件，自身只保留薄薄的属性面与最小能力面。
- **think / act 对偶的 ReAct 循环**：`loop` 驱动 observe→think→act→finish；`think` 组装 prompt 并后台调 LLM，`executor` 单咽喉派发工具调用。
- **双协议命令通道**：XML 与 native tool-use 统一成命令 IR，按端点 wire 协议（OpenAI / Anthropic）自动选信封。
- **统一 LLM 路由**：显式 / task-map / 智能策略三路由，多 provider 抽象，串起成本核算、OAuth 凭证、错误恢复与上下文压缩。
- **双轴权限**：审批轴（要不要问用户）× 沙箱轴（能不能碰这条 path）正交组合，`deny`/`ask` 免 bypass，命令执行必过 classifier。
- **崩溃安全的会话持久化**：追加式 `rollout.jsonl` 为单一真相源，支持 replay 恢复、fork 血缘、文件历史快照（blob / git 双底座）。
- **多 agent 运行时**：事件驱动调度器 + 层级 agent path + per-agent 邮箱 + LRU 驻留淘汰 + cron 定时 + 文件监视。
- **事件总线脊柱**：屏幕（renderer）与磁盘（recorder）由同一事件流喂养，hook 可在生命周期切点拦截。
- **零侵入可观测性与日志**：loguru + trace-id + 装饰器/mixin 自动埋点；Langfuse 追踪默认关、懒加载。

## 快速开始

启动交互式 REPL：

```bash
python -m metagpt.cli                 # 默认 Assistant + 默认工具集
python -m metagpt.cli --model <name> --tools Read,Write,Edit,Bash,Glob,Grep --cwd .
```

- Ctrl+C：turn 进行中 → 中断本轮；prompt 处双击 → 退出。Ctrl+D：退出。
- REPL 内输入 `/help` 查看 slash 命令（agents / sessions / resume / fork）。
- 模型与凭证读 `config.yaml` + 分层配置（见下）。

## 目录布局（分层，自下而上）

| 包 | 层 | 一句话职责 |
|------|------|-----------|
| `common` | 0 基础 | 抽象基类 / 跨层 Protocol / 数据模型 / 配置 / 异常 / 事件 / 钩子 / 日志 / 提示词 / 工具函数 |
| `context` | 1 | LLM 看到什么：消息 CRUD + 两级压缩 + 技能注入（`skills/`）+ per-turn 易变上下文（`turn_context/`） |
| `executor` | 1 | 行动侧引擎：工具单咽喉派发 + 权限沙箱 + 后台任务（`tasks/`）+ MCP |
| `router` | 1 | LLM 模型选型 + 多 provider 抽象 + 成本 / OAuth / 恢复 |
| `session` | 1 | 会话历史的崩溃安全持久化（rollout/replay/snapshot/fork） |
| `parser` | 2 | 命令协议通道：XML ⇄ native tool-use 统一成命令 IR |
| `think` | 2 | 思考侧：组装 prompt + 后台调 LLM → `ThinkResult` |
| `loop` | 2 | ReAct 主循环（observe/think/act/finish） |
| `roles` | 3 | Role 编排核心（schema + state + 惰性组件装配） |
| `environment` | 4 | 多 agent 控制平面 + 调度 + 驻留 + cron + 文件监视 |
| `cli` | 5 入口 | 交互式 REPL / 命令行入口 |
| `memory` | — | 程序记忆 / 语义记忆 / 情景记忆（待实现） |

依赖单向向下，跨层一律经 `common/interface/` 的 Protocol 做依赖倒置：

```
common  ◀──  context / executor / router / session  ◀──  parser / think / loop  ◀──  roles  ◀──  environment  ◀──  cli
```

## 配置

分层配置中心（`common/config/`，9 层优先级栈，低 → 高）：

```
DEFAULT → SYSTEM(/etc) → USER(~/.agentframe|~/.metagpt) → PROJECT(metagpt/config.yaml)
→ WORKDIR(<cwd>/.agentframe, 不受信→剥离凭据) → PROFILE → ENV(AGENTFRAME_/METAGPT_)
→ CLI_FLAG(-c key=value) → PROGRAMMATIC → MANAGED(锁死)
```

dict 深合并、list 并集去重、scalar 高层胜。诊断：`python -m metagpt.common.config.diagnostics --strict`。

## 测试

```bash
python -m pytest metagpt/ztest/{roles,loop,executor,think,context,skills,router,tasks,environment} -q
```

测试在 `metagpt/ztest/<subsystem>/`（不是 `tests/`）。

## 进一步阅读

- [`ARCHITECTURE.md`](./ARCHITECTURE.md) —— 逐包的详尽架构文档 + 全景图 + 一次 turn 的全链路数据流。
- [`AGENTS.md`](./AGENTS.md) —— 在本代码库写代码的约定（分层、工具开发、协议、测试、改动纪律）。
