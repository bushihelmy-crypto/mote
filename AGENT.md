# AgentFrame — Agent（Role）指南

> 本文聚焦框架里的 **Agent 抽象**——也就是 `Role`：它是什么、由什么组成、怎样
> 配置、生命周期如何流转、如何定义/运行一个 agent、如何做多智能体协作。内容基于
> `metagpt/` 源码（`roles/role.py`、`roles/role_schema.py`、`roles/role_state.py`
> 等）逐文件核对，配套见 `ARCHITECTURE.md`。

---

## 1. 什么是 Agent

在 AgentFrame 里，一个 Agent 就是一个 **`Role`** 实例
（`roles/role.py:72`）。它是一个**纯编排器**：

- 不是 Pydantic 模型，而是普通 ABC（`class Role(BaseRole)`）。
- 本身**不持有 LLM**：它持有 `LLMRouter`，谁需要 LLM 谁经 router 现取。
- 本身**不实现工具**：工具是独立的 `BaseTool`，由 `ToolExecutor` 分发。
- 自身只做一件事：驱动 **observe → think → act** 的 ReAct 循环。

一个 Role = **静态配置 `RoleSchema`** + **运行时状态 `RoleState`** + 一组**惰性
子系统**。

```python
from metagpt.roles.role import Role

class Role:
    role_schema: RoleSchema   # 部署期静态配置（Pydantic）
    state: RoleState          # 运行期可序列化快照（Pydantic）
    # 其余全是 Optional 的惰性 slot：router/executor/context_manager/...
```

---

## 2. Agent 的解剖：三块

### 2.1 RoleSchema —— 静态配置（部署期，不变）

`roles/role_schema.py`。常用字段：

| 字段 | 默认 | 含义 |
|---|---|---|
| `name` / `profile` / `goal` / `constraints` / `desc` | `"Zero"`/`"Role"`/... | 身份；`display_name` = `"name(profile)"` |
| `system_prompt` / `cmd_prompt` / `instruction` / `example` | 内置模板 | prompt 模板 |
| `command_protocol` | `"native"` | `"xml"` 文本协议 / `"native"` provider 原生 tool-use |
| `max_react_loop` | `50` | 单次 run 最大 act 轮数 |
| `max_consecutive_react_limit` | `10` | 连续 act 上限（触发后询问人类） |
| `tools` / `mcps` / `agents` / `skills` | `[]` | 声明可用工具 / MCP / 子 agent / 技能 |
| `shell_tool` | `"terminal"` | 声明的 `"Bash"` 解析成持久 `terminal` 还是一次性 `bash` |
| `permissions` | `None` | opt-in `PermissionConfig`（审批层） |
| `hooks` | `None` | opt-in `HookConfig`（生命周期 Hook） |
| `lsp` | `None` | opt-in `LspConfig`（语言服务器诊断） |
| `record_file_history` | `True` | 文件改前快照（undo/diff） |
| `snapshot_backend` | `"auto"` | 快照底座 `auto`/`blob`/`git` |
| `enable_memory` / `memory_k` | `True` / `30` | 记忆开关 / 取最近 k 条 |
| `use_summary` | `True` | end_session 时是否产出总结 |
| `enable_router` | `False` | 是否开启智能路由（按请求选模型） |

> 设计取舍：native 信封（OpenAI/Anthropic）**不**写在 schema，由
> `infer_native_tool_provider(config.llm)` 从 LLM 配置推断，因为它必须匹配实际发
> 请求的 client。

### 2.2 RoleState —— 运行时快照（可序列化、可恢复）

`roles/role_state.py`。关键字段：

| 字段 | 含义 |
|---|---|
| `context: LLMCallContext` | 会话历史（ContextManager 的 backing store；被 checkpoint） |
| `msg_buffer: MessageQueue` | 私有消息缓冲（不序列化） |
| `session_id` | 会话标识（默认 uuid4） |
| `parent_session_id` | fork 血缘（root 为 None） |
| `working_dir` / `original_working_dir` / `project_root` | 三个工作目录（对齐 CC：live cwd 跟随 `cd` / 启动目录 / 项目身份锚） |
| `latest_observed_msg` / `recovered` | 崩溃恢复用 |
| `addresses` / `watch` | 消息路由地址 / 关注的 cause |
| `env` | 所属环境（不序列化） |
| `_active`（PrivateAttr） | think/act 循环的 kill switch 信号 |
| `_file_read_state`（PrivateAttr） | 路径→读时 mtime_ns（Write/Edit 读前校验用） |

`dump()` / `_from_dict()`（`role.py:141`）把 Role 序列化成
`{__module_class_name, state, role_schema}`，供 checkpoint/恢复。

### 2.3 惰性子系统

见 `ARCHITECTURE.md §4`。每个都是 `@property` 惰性构建：`router` /
`context_manager` / `executor` / `think_engine` / `command_channel` /
`context_provider` / `skill_manager` / `bg_pool` / `session_recorder` /
`file_snapshot_recorder` / `hook_manager`(可 None) / `lsp_service`(可 None)。

---

## 3. Agent 生命周期：一次 run

`Role.run(with_message)`（`roles/role.py:720`）即一个 **turn**：

```
1. bind_trace(session_id)              # 该 turn 所有日志关联同一 trace-id
2. _ensure_ready()                     # 惰性建 ContextManager / skills / init MCP
3. [hook] SessionStart                 # 整个 Role 生命周期内仅一次
4. 若有 with_message：
     - 包装成 Message，cause_by=USER_REQUIREMENT，send_to 加自己
     - [hook] UserPromptSubmit         # 可注入额外上下文 / stop 否决本轮
     - lsp.drain_diagnostics()         # 上一轮编辑的诊断回流（prepend，ephemeral）
     - put_message(msg)                # 进 msg_buffer
5. loop = _make_loop()  →  rsp = await loop.run()    # ReActLoop（见架构文档 §3）
6. finally：
     - state.latest_observed_msg = loop.latest_observed_msg   # 恢复用
     - _record_turn_boundary()         # 写 turn_context 事件到 rollout.jsonl
     - [hook] Stop
7. state._active = False；rsp 打上 display_name；publish_message(rsp)
```

**kill switch**：`active` 信号放在 `RoleState` 而非循环内，因为它要兼作
工具→循环的开关——`End` 工具和 `ask_human` 的 "stop" 调 `deactivate()`，必须能打断
正在跑的循环（`role.py:544`）。

---

## 4. 能力注入：工具如何安全地"够到" Role

工具**拿不到** `Role` / `RoleState` / memory。它们只能声明 `requires`，由
`bind()` 对照 `Role.tool_capabilities()`（`role.py:522`）这张**显式白名单**注入少数
窄方法：

```python
def tool_capabilities(self) -> dict[str, Any]:
    return {
        "get_cwd": ..., "set_cwd": ...,            # cwd 读写（cd 持久化）
        "deactivate": ...,                          # 关闭循环
        "ask_human": ..., "request_approval": ...,  # 人类通道
        "reply_to_human": ..., "end_session": ...,
        "record_file_read": ..., "get_file_read_mtime": ...,  # 读前校验
        "record_file_snapshot": ...,                # 改前快照
        "wait_interruptible": ...,                  # Sleep 工具
    }
```

名字不在白名单 → `bind()` 抛 `AttributeError`。`getattr(role, ...)` 从不使用。
这样**角色行为留在 Role，工具保持瘦触发器**。

例：`Bash` 声明 `requires=("get_cwd","set_cwd")`；`Read` 声明
`requires=("record_file_read",)`。

---

## 5. 定义并运行一个 Agent

### 5.1 最小用法（直接 run）

```python
from metagpt.roles.role import Role

role = Role(
    context=my_context,            # 注入的 Context（含 config / LLM 工厂）
    name="Coder",
    role_schema=RoleSchema(
        profile="Engineer",
        goal="实现并测试功能",
        tools=["Bash", "Read", "Write", "Edit", "grep", "glob"],
        command_protocol="native",
    ),
)
rsp = await role.run("帮我修复 login 的 bug")
```

`Role` 的构造支持三种给配置方式（`role.py:84`）：传 `role_schema=`、传
`**schema_kwargs`（自动建 RoleSchema）、或都不传（默认 RoleSchema）。

### 5.2 自定义 Agent 子类

`Role` 是 ABC，但 `run` 已实现；多数情况下**无需子类化**，靠 `RoleSchema` 配置即可。
需要定制时可子类化并覆写（例如换循环策略——目前 `_make_loop()` 总是
`ReActLoop`，未来从 schema 选；`role.py:700`）。

### 5.3 开启高级层（opt-in）

```python
RoleSchema(
    tools=["Bash", "Write", "Edit"],
    permissions=PermissionConfig(mode="acceptEdits", sandbox=SandboxConfig(...)),
    hooks=HookConfig(events={...}),
    lsp=LspConfig(enabled=True, servers=[LspServerConfig(name="pyright", ...)]),
)
```

或用 SDK 风格在 run 前注册 Python hook 回调：

```python
role.register_hook("PreToolUse", my_callback, matcher="Bash")
```

未配置任何层时，对应子系统返回 `None`，所有调用点短路、零开销。

---

## 6. 会话持久化：resume / fork

每个 Role 自动写追加式 `rollout.jsonl`（真相源，`roles/session/`）。

```python
# 列出可恢复会话（按时间倒序，可按 cwd 过滤）
sessions = Role.list_sessions(cwd="/path/to/project")

# 在已构造好（同 session_id）的 Role 上灌入历史
ok = role.resume_session()        # 无日志返回 False

# 从当前会话分叉一个独立兄弟 Role（保留 parent_session_id 血缘）
child = role.fork_session()
```

- **resume**：`replay()` 单次正向扫描重建历史，直接赋值
  `state.context.messages[:]`（绕过 ContextManager.add，避免重复记录）；从
  session_meta 恢复 cwd/project 锚点。
- **fork**：纯磁盘 replay 父日志 → 为子建新 log，子从父最终状态起步，二者独立。

> rollout 只重建**会话历史 + 身份**，不含 RoleSchema（配置）。完整 Role 配置仍靠
> caller 构造或 ResidencyStore 全量快照。

文件历史（改前快照）默认开启：Write/Edit/NotebookEdit 改盘前存 before-image，
经 `roles/session/history.py` 可 diff/restore/list。

---

## 7. 多智能体协作

一个 agent 想拉起子 agent，用内置 `Agent` 工具（别名 `run_agent`，
`executor/tools/agent_tool.py`）。运行时由 `environment/` 控制平面承载：

- 每个 Role 包成 `AgentRuntime`（`environment/runtime.py`）——一个 turn = 一次
  `Role.run()`，对 Role 做 duck-typing（环境层从不 import Role）。
- `AgentControl`（`environment/control.py`）是 session 级控制平面：注册表 +
  并发限流 + LRU 驻留 + 事件调度 + 磁盘物化。
- **无广播**：消息按 `send_to` 路由进各 agent 的 **mailbox**（turn-atomic），
  `send_input`/`send_inter_agent_communication` 投递并唤醒（trigger-turn）或
  排队（queue-only）。子 agent 达终态时 queue-only 通知父 agent。

### 人类通道（MGXEnv）

`Role.ask_human` / `reply_to_human` / `request_approval` 只在
`env` 是 `MGXEnv`（`environment/mgx/`）时生效，背后是 console/human-input hook：

```python
env = MGXEnv(desc="一个软件团队")
env.add_role(role_a)
env.add_role(role_b)
env.publish_message(Message(content="...", send_to={"Coder"}))
await env.run(k=1)        # 泵一次调度
```

`ask_human` 的回复以 "stop" 结尾会触发 `deactivate()`（kill switch）；
`request_approval`（权限审批通道）则**不**因 "stop" 关闭 Role。

### 后台慢任务

工具可返回 `BgTaskResult`，由 `BackgroundTaskPool`（`tasks/pool.py`）异步执行，
完成后把 `BackgroundTaskNotification` 推回 msg_buffer，被下一轮 `_observe()`
自动拾取。`Role.wait_interruptible()` 在新消息或后台任务完成时提前唤醒。

---

## 8. Agent 怎么"看世界"：上下文与 prompt

每个 think 轮由 `ContextProvider`（`roles/context_provider/provider.py`）组装
`ThinkRequest`：

1. `_collect()` → `PromptBuilder.collect_context()` 收集身份、工具 schema、MCP、
   skills、git 状态、memory、环境段等（`prompts/prompt_builder.py`）。
2. `PromptBuilder.build()` 产出 `(system_prompt, user_prompt)`。
3. `ContextManager.prepare_request(user_prompt)` 跑压缩后返回
   `managed_history + [user_prompt]`。
4. `command_channel.tool_specs(executor)` 给出 native tool specs。

**prompt cache 友好**：系统 prompt 有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分界线，
逐轮变动的内容（git 状态、env 段、memory 索引）放在边界**之下**或经 user context
注入，不破坏可缓存的前缀。

---

## 9. 关键不变量（写/改 Agent 时务必遵守）

1. **工具永不触达 RoleState/memory/Role**：只能经 `tool_capabilities()` 白名单。
   新增能力是显式决定。
2. **角色行为留在 Role**：cwd 所有权、文件读状态、kill switch 都在 Role/RoleState，
   工具是瘦触发器。
3. **静态配置 vs 运行时状态分离**：部署期参数进 `RoleSchema`，运行期可变状态进
   `RoleState`（且必须可序列化以支持恢复）。
4. **高级层 opt-in**：permission/hook/lsp/snapshot 未配置时必须短路、零开销、
   不改旧行为。
5. **协议无关**：循环不感知 XML/native，新增协议加 `CommandChannel` 策略即可。
6. **分层单向**：Role 依赖子系统，子系统不反向 import Role；跨层走
   `common/interface` 的 Protocol + 注入。
7. **日志风格**：用 `@log_class` 自动日志，不在方法体手写 `logger.*`。
</content>
