# AGENTS.md — 在本代码库工作的约定

本文件给在 `mote/` 包里写代码的人/agent。内容基于实际源码，不是规划。改动前请先读 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。

---

## 0. 范围

- 工作目录就是 `mote/` 这个 Python 包（`mote.*`），它本身就是完整框架。
- **`mote/` 之外的文件与本项目无关**，不要去读、改、参考（包括仓库根目录里任何同名的 README/ARCHITECTURE/AGENTS）。

---

## 1. 分层是硬约束

依赖必须单向向下，**禁止上层泄漏到下层**：

```
common  ◀──  context / executor / router / session  ◀──  parser / think / loop  ◀──  roles  ◀──  environment  ◀──  cli
```

- 子系统已收敛进所属层：`skills/` 在 `context/skills/`，`tasks/` 在 `executor/tasks/`（含 `bggraph/` DAG），二者不再是顶层包。
- `common/` 是叶子，永不 import 任何上层。
- 低层要用高层能力时，**走 `common/interface/` 里的 Protocol**（`@runtime_checkable`），由高层在装配期注入实例。新增跨层能力 = 先在 `common/interface/` 定义 Protocol，再让高层实现并注入，**不要直接 import**。
- 典型例子：`executor` 要通知 LSP → 依赖 `LspNotifier`；`executor` 文件改写 → 依赖 `FileSnapshotStore`。（会话记录走另一条路：`context` 只往 `common/events` 的 EventBus 发事件，`RecorderSubscriber` 落盘，不经 Protocol。）
- `session` 虽被 `roles`/`environment` 消费，但自身只依赖 `common`，故归在低层。
- `environment → session` 可在模块加载期直接 import（无环）；`environment → roles` 必须惰性 import 打破环。
- 写新源（turn_context source）这类低层组件时，若要 import 高层（如 `executor/tasks`），**把实现放到那个高层包里**（如 `BackgroundTaskContextSource` 放在 `executor/tasks/` 而非 `context/turn_context/`），守住分层。

---

## 2. Role 的组合模型

- `Role` 是**普通 ABC（不是 Pydantic）**。它只做编排，不堆继承。
- 配置进 `RoleSchema`（Pydantic，部署期静态），运行时状态进 `RoleState`（可序列化）。新增"配置项"加到 `RoleSchema`，新增"运行时状态"加到 `RoleState`，不要塞进 `Role` 实例属性。
- 组件都是 `Role` 上经 `RoleComponents` 装配的**惰性初始化属性**（`executor` / `context_manager` / `router` / `think_engine` / `command_channel` / `context_provider` / `turn_context_bus` / `event_bus` / ...）。新组件照此模式加一个 `_xxx` slot + 惰性属性。
- opt-in 子系统（hook / lsp / file_watch / permission）默认惰性属性返回 `None`，仅在配置开启时构造；`turn_context_bus` 例外（总存在，各源自抑制）。

---

## 3. 工具开发

- 在 `executor/tools/` 新建模块，定义 `@register_tool` 的 `BaseTool` 子类即可（`ToolRegistry.discover()` 包扫描自动发现，无需手动注册到 `__init__`）。
- 用 `requires=(...)` **声明需要的能力名**；只有出现在 `Role.tool_capabilities()` 白名单里的能力才会被注入。**工具拿不到整个 `RoleState`/memory/env** —— 这是最小权限边界，别绕过。
  - 现有能力白名单：`get_cwd, set_cwd, deactivate, ask_human, request_approval, reply_to_human, end_session, record_file_read, get_file_read_mtime, record_file_snapshot, wait_interruptible`。新增能力要同时在 `Role.tool_capabilities()` 注册。
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
- 成本统计走 `router/cost/`：`_update_costs` → `TokenUsage.from_usage(...)` → `CostTracker.add(...)`，别只取 prompt/completion（会丢 cache/reasoning token）。

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

- 跨子系统通信走 `common/events/` 的**两平面** `EventBus`：控制订阅者（暴露 `handle_control`，按 `handles` 路由）可折叠 typed `ControlOutcome` 影响宿主（PreToolUse/UserPromptSubmit/PreCompact…）；观察者 fire-and-forget，返回值被丢弃。深层调用点用 `observe_event`/`observe_event_sync` 发**观察**事件（无 bus 时 no-op，结构上带不了控制），不必手动穿线。
- **不要手写 inline `logger.*`**。给关键类加 `@log_class(level="DEBUG", exclude={热路径/平凡 accessor})` 自动埋点。
- 热路径方法（如 ContextManager 的 `get/add/...`）放进 `exclude`，否则日志会被刷爆。
- `bind_trace(session_id)` 已在 `Role.run()` 接好，新代码无需重复绑定。
- tenacity 的 `@retry(..., after=after_log(logger, ...))` 保留 —— 那是配置不是 method-body 日志。

---

## 8. 测试

- 测试在 `mote/ztest/<subsystem>/`（**不是** `tests/`），用 pytest。
- 在范围内跑：

```bash
python -m pytest mote/ztest/{roles,loop,executor,think,context,skills,router,tasks,environment} -q
```

- 改了某子系统，至少跑该子系统 + 其直接依赖方的 ztest，确认无回归。
- 已知预存问题（非新引入）：
  - `mote/ztest/prompts/*` 因测试自身 import 路径错误（`No module named 'prompts'`）收集失败，与应用代码无关。
  - `role_utils.py` 原地 mutate 共享常量 `ASK_HUMAN_COMMAND` 会造成顺序依赖污染。
  - 本机 pytest/py3.11 偶发 `INTERNALERROR AST recursion depth mismatch`，用 `--tb=short`/`--tb=no` 规避。
- 交互式 PTY/kernel 测试：多次调用必须包在**一个 `asyncio.run`** 里（conftest 每次 `run()` 开新 loop 会孤儿化 reader/channel），每个 test 用唯一 session_id + cleanup 防 singleton 泄漏。

---

## 9. 改动纪律

- 只做被要求或明确必要的改动；不顺手重构、不加未要求的"可配置性"、不给没改的代码加注释/类型/docstring。
- 不为不可能发生的场景加防御/兜底/feature flag；只在系统边界（用户输入、外部 API）做校验。
- 不留向后兼容残渣（重命名未用 `_var`、`// removed` 注释、re-export 已删类型）；确认无用就直接删。
- 安全：注意命令注入/路径穿越；命令执行必经 classifier；权限的 deny/ask 不可被 bypass 绕过 —— 别新增绕过路径。
- 危险/不可逆操作（删文件分支、force push、reset --hard、改 CI、装/删依赖、发 PR/消息）默认先与用户确认，除非已被明确授权。

---

## 10. 入口与快速定位

| 我想找… | 去哪 |
| --- | --- |
| 程序入口 / REPL | `cli/__main__.py`、`cli/repl.py`、`cli/commands.py` |
| 主循环 | `loop/react_loop.py` |
| Role 编排 | `roles/role.py`（配置 `roles/role_schema.py`，状态 `roles/role_state.py`，装配 `roles/role_components.py`） |
| 调 LLM | `think/think_engine.py` + `router/llm/base_llm.py` |
| 提示词组装 | `think/prompts/prompt_builder.py` + `common/prompt/` |
| 命令协议（XML/native） | `parser/`（`factory.py`/`native_channel.py`/`xml_channel.py`，ABC 在 `common/base/command_channel.py`） |
| 工具 | `executor/tools/`（基类 `executor/base_tool.py`，分发 `executor/tool_executor.py`） |
| 权限 | `executor/permission/`（`engine.py`/`classifier.py`/`sandbox/`） |
| 后台任务 / DAG | `executor/tasks/`（`pool.py`/`bggraph/`） |
| 模型路由 | `router/router.py` + `router/strategy.py` |
| 上下文/压缩 | `context/manager.py` + `context/autocompact.py`/`microcompact.py` |
| 技能 | `context/skills/` |
| per-turn 注入 | `context/turn_context/`（+ `executor/tasks/turn_context_source.py`） |
| 会话持久化 | `session/`（log/replay/listing/fork/snapshot/history/subscribers） |
| 多 agent | `environment/`（control/runtime/scheduler/residency/store + `scheduling/` cron + `watching/`） |
| 事件总线 | `common/events/` |
| 跨层 Protocol | `common/interface/` |
| 配置 | `config.yaml` + `common/config/` |
