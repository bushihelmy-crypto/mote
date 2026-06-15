# AgentFrame 架构文档

本文档描述 `metagpt/` 包的实际架构，内容基于源码逐文件阅读，而非设计意图或记忆。

---

## 1. 总体设计

AgentFrame 是一个 **Agent 运行框架**：一个统一的 `Role` 在 **ReAct（think → act）循环**里反复调用 LLM、解析命令、执行工具、把结果写回上下文，直到任务结束。

三条贯穿全局的设计原则：

1. **分层 + 单向依赖**：`common/` 是叶子基础层（不向上 import）；上层（`roles`/`executor`/`context`/`loop`/`think`/`router`/`environment`/`session`/`skills`/`tasks`/`parser`/`cli`）只能向下依赖。跨层解耦靠 `common/interface/` 里的 **Protocol（PEP 544 结构化类型）** —— 低层只依赖 Protocol，具体实现由高层注入。
2. **组合优于继承**：`Role` 是一个**普通 ABC（非 Pydantic）**，通过组合 `RoleSchema`（静态配置）+ `RoleState`（可序列化运行时状态）+ 一组惰性初始化的组件来工作，而不是用一个庞大的继承树。
3. **能力注入（capability injection）**：工具用 `requires=(...)` 声明它需要的能力名，`ToolExecutor` 绑定时只把 `Role.tool_capabilities()` 里的那几个窄回调交给它 —— 工具拿不到整个 `RoleState` / memory / env，最小权限。

---

## 2. 分层与依赖

```
            ┌─────────────────────────────────────────────┐
   高层      │  cli                                          │
            ├─────────────────────────────────────────────┤
            │  environment   session   skills   tasks       │
            ├─────────────────────────────────────────────┤
            │  roles                                        │
            ├─────────────────────────────────────────────┤
            │  loop   think   parser                        │
            ├─────────────────────────────────────────────┤
            │  context   executor   router                  │
            ├─────────────────────────────────────────────┤
   叶子      │  common  (base / config / schema / interface  │
            │           / hook / prompt / logs / ...)        │
            └─────────────────────────────────────────────┘
```

- 任何模块都可以 import `common`；`common` 不 import 任何上层。
- `context` 可以依赖 `executor`（向下），反过来不行。
- `session` 是顶层包，只依赖 `common`；`environment → session` 在模块加载期直接 import（无环），`environment → roles` 用惰性 import 打破环。
- 低层需要调用高层能力时，**一律走 `common/interface/` 的 Protocol**，由高层在装配期注入实例。

### `common/interface/` 的 Protocol（解耦核心）

这些 `@runtime_checkable` Protocol 让低层"消费能力"而不 import 实现：

| Protocol | 消费方 | 实现/注入方 |
| --- | --- | --- |
| `LLMClient` | think / loop | router 的 LLM 客户端 |
| `MessageStore` | loop | `context.ContextManager` |
| `RequestAssembler` | loop | `roles.context_provider` |
| `BackgroundPool` | loop | `tasks.BackgroundTaskPool` |
| `SessionRecorder` | context | `session.SessionRecorder` |
| `FileSnapshotStore` | executor | `session.FileSnapshotRecorder` |
| `HookRunner` | context / executor | `common.hook.HookManager` |
| `LspNotifier` | executor | `roles.lsp.LspService` |
| `EphemeralContextSource` | context.turn_context | git/token/bg/lsp 各源 |
| `MessageActivity` | environment | role / runtime |

---

## 3. 核心执行路径

### 3.1 入口装配

`cli/repl.py::build_repl(model, tools, cwd, name)` 装配整条链：

```
Config(分层加载) → Context(config + cost_manager) → Role(RoleSchema + RoleState)
  → AgentRuntime(包一次 Role.run) → AgentControl(控制面) → Repl(REPL 循环)
```

`python -m metagpt.cli`（`cli/__main__.py`）解析 `--model/--tools/--cwd/--name`，调 `build_repl(...)` 再 `asyncio.run(repl.run())`。

### 3.2 一个回合（turn）

```
Repl._run_turn
  └─ AgentControl.send_input(text, TRIGGER_TURN)   # 投递到邮箱并唤醒
       └─ AgentRuntime.run_one_turn
            └─ Role.run(with_message)
                 ├─ bind_trace(session_id)          # 日志 trace-id
                 ├─ SessionStart hook (仅首次)
                 ├─ UserPromptSubmit hook
                 ├─ _make_loop() → ReActLoop
                 ├─ await loop.run()                # ← 核心循环
                 ├─ _record_turn_boundary()         # 写 turn_context 事件
                 └─ Stop hook
```

回合结束后，REPL 从 `role.state.context.messages` 读取最新的 assistant 回复展示给用户。

### 3.3 ReAct 循环（`loop/react_loop.py`）

`ReActLoop` 用**散参注入**（只收可复用组件）：`think_engine / command_channel / executor / memory / context_provider / is_active / set_active / get_bg_pool`。

```
run():
  ctx = context_provider.loop_context()            # 静态 observe + 循环控制束
  if not _observe(): return None                   # 初始 gate：无新消息则退出
  set_active(True)
  while actions_taken < max_react_loop:
    _observe(NEXT)                                  # 拉新消息（重置 consecutive）
    has_todo = _step_think()                        # think
    if not has_todo:
        # 没事做：若后台任务 pending 则等一个完成再 observe，否则 break
    if channel.is_terminal(think_engine):           # 协议感知终止
        rsp = _finish(); break                      # native：模型不再调工具，纯文本收尾
    rsp = _step_act()                               # act：解析命令 → 执行 → record_turn
    actions_taken += 1; consecutive += 1
    # post-check：到达 max 轮数 / 连续上限 → 有 ask_human 则问用户，否则 break
```

- **observe**：从 `msg_buffer` 弹出消息，按 `watch` / 收件人地址 / `MESSAGE_ROUTE_TO_ALL` 过滤，对已存历史去重，写入 `ContextManager`（memory store），记录 `latest_observed_msg`（崩溃恢复用）。
- **think**（`_step_think`）：`active` 关掉就立刻返回 False（`End` 工具会 `deactivate`，从而终止 XML 协议）。否则 `context_provider.prepare()` 组装 `ThinkRequest` → `resolve_llm(req)` 经路由选模型 → `think_engine.start(...)` 起一个后台 asyncio 任务调 LLM。
- **act**（`_step_act`）：经 `channel.iter_commands` 解析出命令列表，**按序执行**；第一个失败后，剩余命令不再执行但仍**记录一个 SKIPPED 结果**（原生 tool-use 要求每个 tool_call 必须有配对的 tool_result，不能直接丢）。最后 `channel.record_turn(memory, content, executed)` 把这一回合按协议形态写入 memory。
- **协议感知终止**：XML 协议在模型发出 `End` 命令（→ deactivate → 下一次 think 返回 False）时结束；native 协议在模型停止调用工具、返回纯文本时结束（`_finish` 把该文本记为最终回合）。

---

## 4. 子系统详解

### 4.1 `roles/` — Role 与编排

- **`role.py` `Role(BaseRole)`**：纯编排类（普通 ABC）。组合：
  - `role_schema: RoleSchema`（静态配置）+ `state: RoleState`（运行时状态）
  - 惰性组件槽：`_think_engine / _executor / _skill_mgr / _bg_pool / _command_channel / _context_provider / _context_manager / _router / _session_recorder / _file_snapshot_recorder / _hook_manager / _lsp_service / _turn_context_bus`
  - 关键惰性属性：`router`（`get_router(context)`）、`executor`（`ToolExecutor`，工具 = `mcps + tools`）、`context_manager`（`ContextManager`，背靠 `state.context`，压缩 LLM = `router.route_for_task(COMPRESSION_TASK)`）、`hook_manager`（无 HookConfig 且无注册回调时为 None）、`lsp_service`（未 enabled+servers 时为 None）、`turn_context_bus`（**总存在**，装配 git/token/bg/lsp 四个源）。
  - `tool_capabilities()` 返回能力白名单：`get_cwd, set_cwd, deactivate, ask_human, request_approval, reply_to_human, end_session, record_file_read, get_file_read_mtime, record_file_snapshot, wait_interruptible`。
  - 会话方法：`resume_session()` / `fork_session()` / `list_sessions()`（静态委托）/ `_record_turn_boundary()`。
- **`role_schema.py` `RoleSchema`（Pydantic）**：部署期静态配置。字段：身份（name/profile/goal/constraints/desc/role_id）、提示词模板（system_prompt/cmd_prompt/instruction/example/summary_prompt/...）、`command_protocol`（`"xml" | "native"`，**实际默认值 `"native"`**）、循环控制（`max_react_loop=50` / `max_consecutive_react_limit=10`）、`tools`（默认 `Read/Write/Edit/Glob/Grep/Bash/Terminal/Jupyter/Agent/AskUserQuestion/Sleep`）、`mcps/agents/skills`、`permissions`（默认 `PermissionConfig`，`default` 模式 = 无匹配 allow 规则就问用户）、`hooks`/`lsp`（均默认 None = 不启用）、`record_file_history=True` / `snapshot_backend="auto"`、memory（`enable_memory/memory_k=30/use_summary`）、`enable_router=False`。属性 `display_name` → `"Zero(Role)"`。
- **`role_state.py` `RoleState`**：可序列化运行时状态。`context: LLMCallContext`（含 `messages`，崩溃恢复真相源）、`msg_buffer`（`exclude=True` 的 in-flight 队列）、`session_id` / `parent_session_id`、`working_dir` / `original_working_dir` / `project_root`、`_active`、`_file_read_state`。
- **`context_provider/provider.py` `ContextProvider`**：为循环打包参数。`loop_context()` 返回静态 `LoopContext`（observe + 循环控制束）；`prepare()` 经 `PromptBuilder` 组装出 `ThinkRequest`（req / system_prompt / state_data / tool_specs）；`resolve_llm(req)` 经 router 选 LLM；`_think_subsystems()` 把 `turn_context_bus` 等交给 PromptBuilder。
- **`lsp/`**：opt-in 的 LSP service（独立长驻子进程 + Content-Length JSON-RPC over stdio）。被动 next-turn 诊断回流（编辑文件 → 下一回合在 `<system-reminder>` 里暴露诊断）。MVP 无交互 LspTool。
- **`agents/`**：极简，仅 re-export `Agent` 工具 —— 子 agent 派生通过 `Agent` 工具完成，而非独立角色类。

### 4.2 `loop/` — ReActLoop

见 §3.3。`ReActLoop(BaseLoop)` 自带迭代状态（`_consecutive`），通过 `is_active/set_active` 读写共享的 `active` 信号（`active` 兼做"工具 → 循环"的 kill switch：`End` 工具 / `ask_human` 的 "stop" 会 `deactivate`）。

### 4.3 `think/` — 思考与提示词

- **`think_engine.py` `ThinkEngine`**：无固定 LLM（每请求由循环解析）。`start(req, system_prompt, state_data, tool_specs, *, llm)` 起后台任务 `_run`；`_run` 据协议调 `_cached_aask_tool`（native）或 `_cached_aask`（XML），再用 `check_duplicates` / `check_duplicate_calls` 去重，产出 `ThinkResult(content, tool_calls)`；`join()` 等任务完成。
- **`prompt_builder.py`**：
  - `ThinkInputs`（纯数据：身份串、env 子句、team、cwd、memory_dir、output_format、command_guide、...）
  - `ThinkSubsystems`（活协作者：config / llm / executor / skill_manager / turn_context_bus）
  - `ThinkContext`（组装好的字段）
  - `PromptBuilder.build()` → `(system_prompt, user_prompt)`。`collect_context(inputs, subsystems)` 异步组装所有字段。
  - **系统提示词缓存边界**：模板里有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记，分隔静态（可缓存）前缀与动态区；发送前去掉标记行。边界**上方无任何 `$placeholder`**，故前缀字节稳定，利于 prompt caching。
  - **MEMORY.md 与 reminders 注入到 user prompt（而非 system）**：避免变动的索引/易变上下文打破系统提示词缓存前缀。`_make_reminders` 调 `turn_context_bus.collect()` 得到 `<system-reminder>` 块。

### 4.4 `context/` — 消息存储与压缩

- **`manager.py` `ContextManager`**：双重身份。
  1. **消息存储**（替代旧 `Memory`，背靠 `RoleState.context` 以便检查点/恢复）：`get / add / add_batch / delete / count / clear`。
  2. **压缩编排**：`manage_history()` 顺序跑两遍 —— PreCompact hook（可否决/注入指令）→ `microcompact`（廉价，无 LLM，原地折叠旧工具结果，报告释放的 token）→ `autocompact`（昂贵，LLM 摘要重建，只在折叠后仍超阈值时触发，用 `tokens_freed` 桥接两遍）→ 替换历史为 `[summary] + tail` → 记录 compaction 检查点到 recorder → PostCompact hook。
  - `token_state()` 经 `token_budget.evaluate` 返回 token 预算快照。
  - `prepare_request(user_prompt, manage=True)` 返回 `stored_history + [user_prompt]`（user prompt 只进请求，不进存储历史）。
- **`microcompact.py`**：`COMPACTABLE_TOOLS = Read/Bash/Grep/Glob/Write/Edit/WebSearch/WebFetch`；把旧的可折叠工具结果体原地折叠。
- **`autocompact.py`**：keep-tail 拆分 + 摘要 + 熔断器（`consecutive_failures`）。
- **`token_budget.py`**：`evaluate(...)` 计算 token 占用、warning/autocompact 阈值。
- **`turn_context/`** — per-turn ephemeral 注入层（统一所有易变上下文）：
  - `bus.py` `TurnContextBus(sources)`：按 `priority` 升序排序，`collect(cwd)` 用 `asyncio.gather` 并发 `render` 所有源，逐源 try/except 隔离，合并成 `<system-reminder>` 块。
  - `format.py` `wrap_system_reminder(blocks)`：非空才包成 `<system-reminder>...</system-reminder>`。
  - 源（实现 `EphemeralContextSource` Protocol）：`git`（priority 10）、`token_pressure`（20，仅超 warning 才发"# Context budget"）、`background_tasks`（30，在 `tasks/turn_context_source.py` 实现以免破坏分层）、`lsp`（40，诊断）。
  - **关键**：这些内容只进 user prompt 的 `<system-reminder>`，**不进可缓存的系统提示词、不进存储历史**。

### 4.5 `executor/` — 工具执行

- **`tool_executor.py` `ToolExecutor`**：`run_command(name, args, result_id)` 流水线：PreToolUse hook → 权限闸门 → 执行工具 → PostToolUse hook → LSP 通知（成功且 `mutates_filesystem` 时 `await lsp_notifier.file_saved(...)`）→ 结果体量限制。`cleanup()` 关闭 terminal/kernel、调 `lsp_notifier.shutdown()`。
- **`base_tool.py` `BaseTool`**：`bind`（注入能力）、`call`、`requires`（能力声明）、`permission_target(s)`、`check_permissions`、自动生成 schema（XML 与 native 两形）。
- **`tool_registry.py`** `@register_tool` + `ToolRegistry.discover()`（包扫描自动发现 `tools/` 下所有 `@register_tool` 子类）；`agent_registry.py` `@register_agent`。
- **`tool_result.py` `ToolResult`**（dataclass：output/success/images/pdfs/...）；`tool_result_limit.py`（超大输出落盘）。
- **内置工具（`tools/`）**：
  - `bash.py` `Bash`：一次性子进程（jam-proof，每调用新进程，仅 cwd 持久，探针 pwd 同步），输出上限 ~30k，`check_permissions` 过分类器。
  - `terminal.py` `Terminal`：**持久 PTY 终端**（每 session 一个隐式终端，无 model-facing id；`input/interrupt(Ctrl-C)/close`；cwd/env/venv 持久；前台程序持有终端）。
  - `python.py` `Jupyter`：**持久 Jupyter kernel**（每 session 一个隐式 kernel；`code/interrupt/restart/close/timeout`；block-to-idle 或 timeout 自动 interrupt 保 partial）。
  - 文件工具（`dependency/_file_base.py FileMutatingTool`：读后写、保留换行、写盘前快照）：`read.py`/`write.py`/`edit.py`/`notebook_edit.py`/`apply_patch.py`。
  - 检索：`grep.py`/`glob.py`。
  - 人机：`human.py`（Ask/Reply/AskUserQuestion）。控制：`end.py`（`End`）、`sleep.py`（`Sleep`）。
  - 派生：`agent_tool.py`（`Agent`，派生子 agent）。
- **`permission/`** — 三轴权限：
  - `types.py`：`PermissionMode`（default/acceptEdits/plan/bypass）、`Decision`（allow/ask/deny）、`Rule`。
  - `engine.py`：多步流水线，**deny/ask 不可被 bypass 绕过**（bypass 只放宽默认 ask）。
  - `classifier.py`：命令安全分类（codex command_safety 的移植，危险表更保守）。
  - `sandbox/guard.py`：full / read-only / workspace-write。
- **`mcp/`**：`universal.py` / `mcp_adapter.py`（把 MCP server 的工具桥接成 BaseTool）。

### 4.6 `parser/` — 命令协议 channel

- `common.base.CommandChannel`（ABC）。
- `xml_channel.py` `XmlCommandChannel`：XML 文本协议（命令块 + `OUTPUT_SECTION`；`<end></end>` 机制）。
- `native_channel.py` `NativeToolChannel`：原生 tool-use（JSON-Schema tool specs + tool_calls）。`infer_native_tool_provider(model)`：model 含 `claude` → `"anthropic"`，否则 `"openai"`（决定 tool-spec 信封形态）。`make_command_channel(protocol, provider=...)` 工厂。
- channel 还提供 `command_guide()`（XML 返回 `<end>` 语义说明，native 返回 tool-call 语义说明，门控避免 `<end>` 泄漏到 native 正文）和 `record_turn(memory, content, executed)`（按协议形态写 memory：XML = 文本 + 合并输出；native = tool_calls + 逐 call tool_result）。

### 4.7 `router/` — 模型路由、LLM 客户端、成本

- **`router.py` `LLMRouter`**：`route` / `route_for_task` / `aroute_decision` / `aroute`。任务常量 `COMPRESSION_TASK="compression"`、`SUMMARY_TASK="summary"`。`get_router(context)` / `LLM()` 工厂。三种路由：显式、任务映射、智能（按请求选 model card）。
- **路由策略**：`strategy.py`（RuleBased / Complexity / LLMJudge）、`complexity.py`（词法/结构/上下文信号 + 分级阈值 HIGH=8/MEDIUM=4）、`squilla.py`（ML 路由）、`ml/`（engine/predictor/inference）。
- **`llm/`**：
  - `base_llm.py` `BaseLLM`：`aask`（XML，默认 stream）/`aask_tool`（native，默认 `stream=True`）、`_build_messages`、多模态、`_update_costs`。`_achat_completion_stream_tool` 默认回退到非流式，供无流式 provider 透明降级。
  - `openai_api.py`：OpenAI 兼容客户端（含流式 tool-use 累积重建 rsp）。
  - `anthropic_api.py` `AnthropicLLM`：原生 `api.anthropic.com/v1/messages`。`_convert_messages` 把 OpenAI wire 转 Anthropic（system 抽出、`role:tool` → `tool_result` block、`tool_calls` → `tool_use` block、合并连续同 role）。`@register_provider([LLMType.ANTHROPIC])`。
  - `llm_provider_registry.py`：`@register_provider` + `resolve_api_type(config)`（显式 anthropic 或 base_url 含 `anthropic.com` → ANTHROPIC，否则保持 config）。
  - `context.py` `Context`：config + `cost_manager: CostTracker`。
- **`cost/`**（替代旧 CostManager）：`usage.py TokenUsage`（input/cached/cache_creation/output/reasoning/total + `from_openai`/`from_anthropic`/`from_usage` 适配）、`pricing.py`（per-Mtok 分层定价 + 最长包含匹配 + 未知模型兜底）、`tracker.py CostTracker`（per-model usage + total_cost + Codex 风格 `context_remaining`）、`report.py`（`/cost` 格式化 + status-line）。
- **`oauth/`**：bearer token 取代静态 api_key。

### 4.8 `session/` — 会话持久化

追加式 JSONL 事件日志为崩溃安全真相源（吸取 codex + claude-code）。

- **`events.py`**：`SCHEMA_VERSION=1`，tagged-union 事件 `SessionMetaEvent / MessageEvent / CompactedEvent / TurnContextEvent / MetaUpdateEvent / FileSnapshotEvent`，行格式 `{type, ts, payload}`。
- **`log.py` `SessionLog`**：`{workspace}/.agent_sessions/{session_id}/rollout.jsonl`。`create(meta)`（已存在则 no-op）/`append(event)`（O_APPEND + flush）/`iter_raw()`（容错读）。
- **`replay.py` `replay(log)`**：**单次正向扫描**重建历史（`message` → append；`compacted` → 历史 RESET 为检查点的 `replacement_history`；其余忽略）。最终历史 = 最后检查点 + 其后 message。
- **`listing.py`**：claude-lite 策略列会话（**不全量 parse**，只读 HEAD 16 行 + TAIL 64KB），按 mtime 倒序，可按 cwd 过滤。
- **`fork.py`**：纯磁盘 fork（replay 父 → 为子写 session_meta（`parent_session_id`）→ 逐条 append 继承历史）。
- **`snapshot.py`**：文件改动前 before-image 内容寻址快照。`BlobStore`（sha256，路径分片）或 `GitBlobStore`（独立 bare git object db，sha1，更省盘）。`detect_blob_backend(cwd)`（在 git 工作树 + 有 git 二进制 → git，否则 blob）。`FileSnapshotRecorder` 实现 `FileSnapshotStore` Protocol。
- **`history.py`**：读侧 `file_history` / `diff_snapshot` / `restore`（正向扫描 rollout + blob 取回）。
- **`recorder.py` `SessionRecorder`**：注入 ContextManager 的 sink，`record_message` / `record_compaction`，best-effort 不抛。

### 4.9 `environment/` — 多 agent 运行时

codex 风格控制面 + 邮箱 + 事件驱动调度。

- **`runtime.py` `AgentRuntime`**：包一次 `Role.run`。`AgentStatus` 枚举、`run_one_turn`、`is_unloadable`。
- **`control.py` `AgentControl`**：`send_input`（`TRIGGER_TURN` / `QUEUE_ONLY`）、`_ensure_loaded`（按需 rehydrate）、`start_completion_watcher`。
- **`base_env.py` `AgentEnvironment`**：`publish_message`、`_resolve_recipients`。
- **`registry.py` `AgentRegistry`** + `SpawnReservation`；**`mailbox.py` `Mailbox`**（`DeliveryMode`、`InterAgentCommunication`）；**`limiter.py` `AgentExecutionLimiter`**。
- **`scheduler.py` `EventDrivenScheduler`**：`_driver` 在 `wake_event` 上 park，`_stage_mailbox` 在回合边界 drain。
- **`residency.py` / `store.py`**：LRU 驻留 —— idle agent 落盘 `.agent_residency/{sid}.json`、按需复活。`ResidencyStore.materialize`（rollout 存在时 `_strip_history` 清空历史，避免与 rollout 双写）/ `rehydrate`（`session.replay` 把历史灌回）。
- **`scheduling/`**：`CronTask` / `CronScheduler` / `CronService`（mtime+size 轮询热重载，不引 watchdog）。
- **`watching/`**：`FileWatcher`（mtime+size 轮询）+ `FileWatchService`（接 HookManager 发 `FileChanged`）。
- **`mgx/mgx_env.py` `MGXEnv`**：`ask_human`（`get_human_input`）/ `reply_to_human`。

### 4.10 `skills/` / `tasks/` / `memory/`

- **`skills/`**：`SkillDefinition`（Pydantic，name `^[a-z0-9-]{1,64}$`）、`skill_pool.py`（从 `yamls/` rglob `SKILL.md` 加载）、`skill_injector.py`（`build_content(max_tokens)` 注入系统提示词）、`skill_manager.py`。
- **`tasks/`**：`pool.py BackgroundTaskPool`（`submit/wait_any/cancel`，`_on_done` 构造 `<task-notification>`）、`attachment.py TaskAttachmentGenerator`、`turn_context_source.py BackgroundTaskContextSource`（priority 30，实现 `EphemeralContextSource`）。
- **`memory/`**：记忆模块（`episodic_memory` / `procedural_memory` / `semantic_memory`）。注：会话历史的真相源是 `RoleState.context.messages` + rollout，由 `ContextManager` 管理。

### 4.11 `common/` — 叶子基础层

- **`base/`**：`BaseRole`（带注册表）、`BaseLoop` + `LoopContext`（dataclass）、`BaseThinkEngine`、`CommandChannel`（ABC）、`BaseAgent`、`Singleton`。
- **`config/`**：`Config`（Pydantic）、分层 loader（`ConfigSource` 优先级 + `deep_merge` + list 替换/并集策略）、env 映射（`AGENTFRAME_*`/`METAGPT_*`）、CLI `-c` 覆盖、`LLMConfig`（含 `LLMType` 枚举，新增 `ANTHROPIC`）。
- **`interface/`**：见 §2 的 Protocol 表。
- **`hook/`**：`HookManager`、`HookEvent` 枚举、决策折叠（deny > ask > allow）、`_MATCH_FIELD`（matcher 字段映射，如 FileChanged 按 path）。
- **`schema/`**：惰性 `__getattr__` 导出 `Message/AIMessage/UserMessage/SystemMessage`、`ThinkResult`、`TokenState`、`ContextManagerConfig`、`LLMCallContext`、`HookConfig`、`LspConfig`、`PermissionConfig` 等。
- **`prompt/`**：`role.py`（`SYSTEM_PROMPT` + `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` + 各 section 模板）、`output.py`（`OUTPUT_SECTION` + command guide 常量）、`memory.py`、`compaction.py`、`agent.py`。
- **`logs/`**：loguru 封装、`@log_class`（自动包裹类的公共方法）、`bind_trace` / `current_trace_id`、`log_llm_stream`。
- **`exception/`**：`MetaGPTError` 层级、`is_retryable`、`classify_llm_error`（循环 openai/anthropic 两 SDK 的 typed error）。
- **`const/`**、**`utils/`**（token_counter 等）、**`git_state/`**（filesystem-first 只读 git 状态注入，TTL 1.5s cache）、**`observability/`**（langfuse_integration、`maybe_span`/`maybe_trace`）。

---

## 5. 关键横切机制

### 5.1 命令协议（XML vs native）

`RoleSchema.command_protocol` 决定协议（**实际默认 `"native"`**）。native 的 tool-spec 信封（OpenAI vs Anthropic）**不在 schema 配置**，而是运行时由 LLM 配置经 `infer_native_tool_provider` 推断（必须匹配真正发请求的客户端）。

- **XML**：模型在文本里发命令块，`End` 命令终止；`command_guide` 教 `<end></end>` 机制。
- **native**：模型用 tool_calls，停止调工具即终止；`command_guide` 教 tool-call 机制（无 `<end>`）。

### 5.2 流式

`aask` / `aask_tool` 默认 `stream=True`。token 经 `log_llm_stream` → 全局 sink → REPL 的 renderer（rich.Live 边流式边渲 Markdown）。非流式输出前先 `end_stream()` 收尾 Live，避免抢屏。

### 5.3 上下文缓存友好

系统提示词以 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分隔静态前缀（无 `$placeholder`，字节稳定，可被 provider 缓存）与动态区。**易变内容（MEMORY.md、git/token/bg/lsp 提醒）一律注入 user prompt 的 `<system-reminder>`**，永不进系统提示词、永不进存储历史。

### 5.4 权限不可绕过

`PermissionEngine` 中 deny/ask 规则不受 `bypass` 模式影响（bypass 只放宽"默认 ask"为"默认 allow"）。命令类工具额外过 `classifier` 安全分类。工具还能在自身 `check_permissions` 里做细粒度自检。

### 5.5 崩溃恢复与会话连续性

`RoleState.context.messages` 是历史真相源且可序列化；同时 rollout.jsonl 增量持久化。`resume_session` 用 replay 把历史灌回已构造好的 Role（绕过 `add` 以免重复记录）。`fork_session` 在磁盘 fork 出独立子会话。ResidencyStore 用 rollout 作历史唯一真相源（materialize 时剥离内存历史，rehydrate 时 replay 灌回）。

---

## 6. 日志与可观测性

- 用 `@log_class(level="DEBUG", exclude={...})` 给关键类自动埋点（入口/出口/耗时/异常），**不手写 inline `logger.*`**。已装饰：`Role / ReActLoop / ToolExecutor / ThinkEngine / ContextManager / SkillManager / LLMRouter / BackgroundTaskPool / AgentRuntime`。
- `bind_trace(session_id)` 在 `Role.run()` 绑定 trace-id，core logger patcher 给每条记录打戳。
- Langfuse LLM tracing 经 `observability/langfuse_integration` 的 `maybe_span` / `maybe_trace`。
- 成本/token 经 `router/cost/`（`base_llm._update_costs` → `CostTracker.add`）。

---

## 7. 配置

唯一必填项是 `llm`，其余均有默认值。加载优先级（低 → 高）：

```
defaults(0) < system(10) < user(20) < project(30) < workdir(35,不受信任) <
profile(40) < env(50) < cli(60) < programmatic(70) < managed(80)
```

- 高优先级层覆盖低优先级层；list 字段按策略替换或并集去重。
- `workdir` 层不受信任：凭据字段（api_key 等）会被剥离。
- env 映射：`AGENTFRAME_LLM__BASE_URL=...` → `llm.base_url`（`__` 分隔层级，值按 YAML 解析）。
- 运行时 `-c key=value` 走 CLI 层。

完整模板见 `config.yaml`（含 LLM、路由、压缩/总结模型、exp_pool、role_zero、MCP、langfuse、sentry 等段，均带中文注释）。
