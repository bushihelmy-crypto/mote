# AGENTS.md — 在本代码库工作的约定

本文件给在 `mote/` 包里写代码的人/agent。内容基于实际源码，不是规划。改动前请先读 [`ARCHITECTURE.md`](./zdocs/ARCHITECTURE.md)。

---

## 0. 范围

- 工作目录就是 `mote/` 这个 Python 包（`mote.*`），它本身就是完整框架。
- **`mote/` 之外的文件与本项目无关**，不要去读、改、参考（包括仓库根目录里任何同名的 README/ARCHITECTURE/AGENTS）。

---

## 1. 分层是硬约束

依赖必须单向向下，**禁止上层泄漏到下层**：

```
contracts <- kernel <- runtime <- orchestration <- product
```

- `contracts/` 只放跨边界数据、ID、事件、错误和 Protocol；不得依赖其他层。
- `kernel/` 只放单 Agent 的 Flow、Think、Parser、prompt 和模型无关执行语义。
- `runtime/` 负责 IO、模型客户端、工具执行、权限、会话、恢复、持久化和日志。
- `orchestration/` 负责多 Agent、后台任务、调度、配额和并发控制。
- `product/` 负责 Coding Agent、内置 Toolsets、Skills、LSP、集成和 CLI。
- 低层需要高层能力时，在 `contracts/ports/` 定义 Protocol，由高层在装配期注入。禁止反向 import。
- `common/` 已删除且架构测试禁止重建；不要建立新的 generic utils 包。

---

## 2. Role 的组合模型

- `Role` 是**普通 ABC（不是 Pydantic）**。它只做编排，不堆继承。
- 配置进 `RoleSchema`（Pydantic，部署期静态），运行时状态进 `RoleState`（可序列化）。新增"配置项"加到 `RoleSchema`，新增"运行时状态"加到 `RoleState`，不要塞进 `Role` 实例属性。
- 组件都是 `Role` 上经 `RoleComponents` 装配的**惰性初始化属性**（`executor` / `context_manager` / `router` / `think_engine` / `command_channel` / `context_provider` / `turn_context_bus` / `event_bus` / ...）。新组件照此模式加一个 `_xxx` slot + 惰性属性。
- opt-in 子系统（hook / lsp / file_watch / permission）默认惰性属性返回 `None`，仅在配置开启时构造；`turn_context_bus` 例外（总存在，各源自抑制）。

---

## 3. 工具开发

- 内置工具放在 `product/toolsets/builtin/`；执行基础设施和工具契约分别放在 `runtime/tools/` 与 `contracts/tools/`。
- 用 `requires=(...)` **声明需要的能力名**；只有出现在 `Role.tool_capabilities()` 白名单里的能力才会被注入。**工具拿不到整个 `RoleState`/memory/env** —— 这是最小权限边界，别绕过。
  - 现有能力白名单：`get_cwd, set_cwd, deactivate, ask_user, request_approval, reply_to_user, end_session, record_file_read, get_file_read_mtime, record_file_snapshot, wait_interruptible`。新增能力要同时在 `Role.tool_capabilities()` 注册。
- 文件改写工具继承 `dependency/_file_base.py::FileMutatingTool`：自动做读后写校验、保留换行、写盘前 `record_file_snapshot`（before-image）。写盘前记得调 `_snapshot_pre_write(full_path)`。
- 命令类工具（Bash/Terminal）的 `check_permissions` 要过 `permission/classifier.py`。
- 工具返回 `ToolResult`（output/success/images/pdfs/...）。超大输出由 `tool_result_limit.py` 落盘换 `<persisted-output>`，别自己截断丢信息。
- 持久会话工具（Terminal/Python）是 per-session 隐式 singleton（无 model-facing id），引擎在 `executor/dependency/_terminal.py` / `_kernel.py`。

---

## 4. 命令协议与 LLM

- 协议由 `RoleSchema.command_protocol` 选（`"native"` 默认 / `"xml"`），channel 由 `parser/factory.py::make_command_channel` 构造。
- native 的 provider 信封（OpenAI vs Anthropic）**不配置**，由 `infer_native_tool_provider` 按 `resolve_api_type` 的 **wire 协议**（端点 transport，非 model 名）推断——Claude 经 OpenAI 网关时仍用 OpenAI envelope。
- 新增 LLM provider：实现 `BaseLLM` 子类 + `@register_provider([...])`，在 `router/llm/__init__.py` import 触发注册；按需 override `get_choice_text` / `get_choice_tool_calls` / 流式方法。`resolve_api_type` 控制自动识别。
- `aask`（XML）/`aask_tool`（native）默认 `stream=True`。无流式 provider 透明降级（基类 `_achat_completion_stream_tool` 回退非流式）。
- 成本统计走 `runtime/models/cost/`：`_update_costs` → `TokenUsage.from_usage(...)` → `CostTracker.add(...)`，别只取 prompt/completion（会丢 cache/reasoning token）。

---

## 5. 上下文与缓存

- 系统提示词以 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分隔：**边界上方是静态可缓存前缀，绝不能放 `$placeholder` 或易变内容**。
- 易变 / per-turn 内容（MEMORY.md、git/token/压缩通知/bg/lsp 提醒）**注入 user prompt 的 `<system-reminder>`**，经 `context/turn_context/` 的 bus。新增一类 per-turn 上下文 = 写一个实现 `EphemeralContextSource` 的源（带 `name`/`priority`/`async render`），挂到 bus，**不要**塞进系统提示词或存储历史。
- 压缩在 `ContextManager.manage_history()`：先 `microcompact`（无 LLM 折叠旧工具结果）再 `autocompact`（LLM 摘要）。哪些工具结果可折叠由工具自声明的 `reconstructable` ClassVar 决定（Role 从 live executor 派生注入）。带熔断防失败循环。

---

## 6. 会话持久化

- rollout.jsonl（`session/`）是历史的崩溃安全真相源。会话记录现在是 **EventBus 订阅者**（`RecorderSubscriber`），不再是注入 ContextManager 的 sink。新增事件类型加到 `events.py`（tagged-union），`replay.py` 的未知 type 会被忽略（向后兼容）。
- 改动若涉及历史重建，注意 `replay` 是单次正向扫描，`compacted` 事件会把历史 RESET 为检查点的 `replacement_history`。
- resume 是"往已构造的 Role 灌历史"（绕过 `ContextManager.add` 以免重复记录），不是凭空造 Role。rollout 只重建"历史 + 身份"，**不含配置（RoleSchema）**。

---

## 7. 事件与日志

- 事件数据在 `contracts/events/`，具体双平面 EventBus 在 `runtime/events/`。Kernel 仅通过 `kernel.telemetry` 的注入能力发观察事件。
- **不要手写 inline `logger.*`**。给关键类加 `@log_class(level="DEBUG", exclude={热路径/平凡 accessor})` 自动埋点。
- 热路径方法（如 ContextManager 的 `get/add/...`）放进 `exclude`，否则日志会被刷爆。
- `bind_trace(session_id)` 已在 `Role.run()` 接好，新代码无需重复绑定。
- tenacity 的 `@retry(..., after=after_log(logger, ...))` 保留 —— 那是配置不是 method-body 日志。

---

## 8. 测试

- 测试在 `mote/ztest/<subsystem>/`（**不是** `tests/`），用 pytest。
- 在范围内跑：

```bash
python -m pytest mote/ztest/{roles,flow,executor,think,context,skills,router,tasks,environment} -q
```

- 改了某子系统，至少跑该子系统 + 其直接依赖方的 ztest，确认无回归。
- 已知预存问题（非新引入）：
  - `mote/ztest/prompts/*` 因测试自身 import 路径错误（`No module named 'prompts'`）收集失败，与应用代码无关。
  - `role_utils.py` 原地 mutate 共享常量 `ASK_USER_COMMAND` 会造成顺序依赖污染。
  - 本机 pytest/py3.11 偶发 `INTERNALERROR AST recursion depth mismatch`，用 `--tb=short`/`--tb=no` 规避。
- 交互式 PTY/kernel 测试：多次调用必须包在**一个 `asyncio.run`** 里（conftest 每次 `run()` 开新 loop 会孤儿化 reader/channel），每个 test 用唯一 session_id + cleanup 防 singleton 泄漏。

---

## 9. 改动纪律

- **除 `ztest/` 外，所有 import 必须位于模块顶部。** 禁止在函数、方法或类体内延迟 import；可选依赖、平台依赖用模块顶部的 `try/except ImportError`，纯类型依赖用模块顶部的 `TYPE_CHECKING`。若模块顶部导入暴露循环依赖，必须通过调整分层、拆模块或在 `contracts/ports/` 抽取 Protocol 消除循环，禁止用局部 import 回避。
- 只做被要求或明确必要的改动；不顺手重构、不加未要求的"可配置性"、不给没改的代码加注释/类型/docstring。
- 不为不可能发生的场景加防御/兜底/feature flag；只在系统边界（用户输入、外部 API）做校验。
- 不留向后兼容残渣（重命名未用 `_var`、`// removed` 注释、re-export 已删类型）；确认无用就直接删。
- 安全：注意命令注入/路径穿越；命令执行必经 classifier；权限的 deny/ask 不可被 bypass 绕过 —— 别新增绕过路径。
- 危险/不可逆操作（删文件分支、force push、reset --hard、改 CI、装/删依赖、发 PR/消息）默认先与用户确认，除非已被明确授权。

---

## 10. 入口与快速定位

| 我想找… | 去哪 |
| --- | --- |
| 程序入口 / CLI | `product/cli/` |
| Agent 执行流 | `kernel/flow/` |
| Role 与装配 | `runtime/agent/` |
| Think / Parser | `kernel/think/`、`kernel/parser/` |
| 工具执行 / 内置工具 | `runtime/tools/`、`product/toolsets/builtin/` |
| 权限与沙箱 | `runtime/tools/permission/`、`runtime/sandbox/` |
| 后台任务 / DAG | `orchestration/tasks/` |
| 模型路由与客户端 | `runtime/models/`、`product/integrations/models/` |
| 上下文 / Skills | `runtime/context/` |
| 会话持久化 | `runtime/session/` |
| 多 Agent | `orchestration/environment/` |
| 事件契约 / 总线 | `contracts/events/`、`runtime/events/` |
| 跨层 Protocol | `contracts/ports/` |
| 配置 | `contracts/config/`、`runtime/config/` |
