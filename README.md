# AgentFrame

> 一个重构后的 Agent 框架（内部代号 *AgentFrame*，包名仍为 `metagpt.*`）。核心理念：
> 把"会思考、会用工具、能多智能体协作"的 Agent 拆成**纯编排的 `Role`** + 一组
> **惰性初始化的子系统**，分层单向、能力注入、高级层 opt-in。
>
> 详细设计见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)（系统架构）与
> [`AGENT.md`](./AGENT.md)（Agent/Role 使用指南）。

---

## 设计要点

- **组合优于继承**：`Role` 不再是 Pydantic 模型，而是普通 ABC，只做编排，能力全部委托给子系统。
- **静态配置 / 运行时状态分离**：`RoleSchema`（部署期静态配置）与 `RoleState`（运行期可序列化快照）严格分离。
- **能力注入**：工具拿不到 `Role`/`RoleState`/memory，只能经 `Role.tool_capabilities()` 白名单拿到少数窄方法。
- **协议无关的 ReAct 循环**：think→act 循环不关心 XML 还是 native tool-use，由 `CommandChannel` 策略隔离。
- **opt-in 高级层**：权限、Hook、LSP、会话持久化、文件快照默认关闭、按需开启，未配置时短路、零开销。
- **严格单向分层**：`common/` 永不 import 高层；跨层协作走 `common/interface/` 的 `Protocol` + 依赖注入。

---

## 整体架构

```mermaid
flowchart TB
    subgraph ENV["environment/ — 多智能体控制平面"]
        Control["AgentControl<br/>注册表/限流/驻留/调度"]
        Runtime["AgentRuntime<br/>一次 turn = 一次 Role.run()"]
        MGX["MGXEnv<br/>人类通道"]
        Cron["scheduling/ cron"]
        Watch["watching/ 文件监听"]
    end

    subgraph ROLE["roles/ — Role 编排层"]
        Role["Role（纯编排 ABC）"]
        Schema["RoleSchema（静态配置）"]
        State["RoleState（运行时快照）"]
        Provider["ContextProvider<br/>唯一持有整个 Role"]
        Session["session/<br/>rollout.jsonl + resume/fork + 文件快照"]
        Lsp["lsp/（opt-in 诊断）"]
    end

    subgraph CORE["核心子系统"]
        Loop["ReActLoop<br/>observe→think→act"]
        Think["ThinkEngine<br/>LLM 调用 + 去重"]
        Exec["ToolExecutor<br/>工具分发单一咽喉"]
        Ctx["ContextManager<br/>消息存储 + 压缩"]
        Router["LLMRouter<br/>显式/任务/智能 路由 + cost"]
        Channel["CommandChannel<br/>XML / native 协议策略"]
        Skill["SkillManager"]
        BgPool["BackgroundTaskPool"]
    end

    subgraph COMMON["common/ — 最底层"]
        Iface["interface/（Protocol）"]
        Base["base/（抽象基类）"]
        Hook["hook/（opt-in 生命周期 Hook）"]
        SchemaPkg["schema / git_state / logs / observability"]
    end

    Runtime -. duck-typing .-> Role
    MGX --> Role
    Role --> Schema
    Role --> State
    Role --> Provider
    Role --> Session
    Role --> Lsp
    Role --> Loop
    Provider --> Loop
    Loop --> Think
    Loop --> Exec
    Loop --> Ctx
    Loop --> Channel
    Think --> Router
    Ctx --> Router
    Role --> Skill
    Role --> BgPool
    Exec --> Hook
    Exec --> Iface
    Ctx --> Iface
    Session --> Iface
    CORE --> COMMON
    ROLE --> COMMON
```

### 包分层与依赖方向

```mermaid
flowchart TB
    L4["environment/<br/>多智能体控制平面 / 运行时"]
    L3["roles/<br/>Role 编排 + session/ + lsp/"]
    L2["loop/ think/ executor/ context/ router/<br/>parser/ prompts/ skills/ tasks/"]
    L1["common/<br/>schema / base / interface / hook / git_state / logs"]

    L4 --> L3 --> L2 --> L1
    L1 -. "Protocol + 依赖注入<br/>（反向协作只走 interface）" .-> L2
```

> 依赖只允许从上往下；反向协作通过 `common/interface/` 的 Protocol（`HookRunner` /
> `SessionRecorder` / `FileSnapshotStore` / `LspNotifier` / `LLMClient` ...）+
> 依赖注入完成。例如 `environment/` 永不 import `roles.Role`，而是对 Role 做
> duck-typing。

---

## 一次 `Role.run()` 的执行流

```mermaid
sequenceDiagram
    autonumber
    participant U as 调用方
    participant R as Role
    participant H as HookManager（opt-in）
    participant L as ReActLoop
    participant P as ContextProvider
    participant T as ThinkEngine
    participant E as ToolExecutor
    participant C as ContextManager

    U->>R: run(with_message)
    R->>R: bind_trace(session_id) + _ensure_ready()
    R->>H: fire(SessionStart) 一次
    R->>H: fire(UserPromptSubmit) 可注入/否决
    R->>R: lsp.drain_diagnostics() 诊断回流
    R->>L: loop.run()
    loop 每轮 react
        L->>L: _observe() 过滤并写入 memory
        L->>P: prepare() 组装 ThinkRequest
        P->>C: prepare_request() 压缩历史 + user_prompt
        P->>R: resolve_llm() 经 router 选模型
        L->>T: start() 后台 LLM 调用
        alt 终止（XML 发 End / native 无 tool_call）
            L-->>R: 返回最终响应
        else 执行命令
            L->>E: run_command(name,args)
            E->>H: PreToolUse → 权限门 → call → PostToolUse → LSP
            E-->>L: ToolResult
        end
    end
    R->>R: _record_turn_boundary() 写 rollout
    R->>H: fire(Stop)
    R->>U: publish_message(rsp)
```

---

## 快速上手

```python
from metagpt.roles.role import Role
from metagpt.roles.role_schema import RoleSchema

role = Role(
    context=my_context,                 # 注入 Context（含 config / LLM 工厂）
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

多智能体（带人类通道）：

```python
from metagpt.environment.mgx.mgx_env import MGXEnv
from metagpt.common.schema import Message

env = MGXEnv(desc="一个软件团队")
env.add_role(role_a)
env.add_role(role_b)
env.publish_message(Message(content="...", send_to={"Coder"}))
await env.run(k=1)                       # 泵一次调度
```

开启 opt-in 高级层：

```python
RoleSchema(
    tools=["Bash", "Write", "Edit"],
    permissions=PermissionConfig(mode="acceptEdits", sandbox=SandboxConfig(...)),
    hooks=HookConfig(events={...}),
    lsp=LspConfig(enabled=True, servers=[LspServerConfig(name="pyright", ...)]),
)
```

会话恢复 / 分叉：

```python
sessions = Role.list_sessions(cwd="/path/to/project")  # 列出可恢复会话
role.resume_session()                                   # 灌入历史
child = role.fork_session()                             # 分叉独立兄弟 Role
```

---

## 目录速览

| 包 | 职责 |
|---|---|
| `roles/` | `Role` 编排 + `RoleSchema`/`RoleState` + `session/`（持久化）+ `lsp/` |
| `loop/` | `ReActLoop`：observe→think→act 循环 + 协议无关终止 |
| `think/` | `ThinkEngine`：封装 LLM think 调用、流式、去重 |
| `executor/` | `ToolExecutor` 分发咽喉 + `BaseTool` + `tools/` + `permission/` + `mcp/` |
| `context/` | `ContextManager`：消息存储 + microcompact/autocompact 压缩 |
| `router/` | `LLMRouter` 三路由 + `cost/` 成本统计 |
| `parser/` | `CommandChannel`：XML vs native tool-use 协议策略 |
| `prompts/` | `PromptBuilder`：纯函数 prompt 组装（cache 友好分界） |
| `skills/` | `SkillManager` + skill 注入 |
| `tasks/` | `BackgroundTaskPool`：慢命令后台执行 + 完成通知 |
| `environment/` | 多智能体控制平面：runtime/mailbox/scheduler/residency + `scheduling/` + `watching/` |
| `common/` | `schema` / `base`（抽象基类）/ `interface`（Protocol）/ `hook` / `git_state` / `logs` |

---

## 测试

测试位于 `metagpt/ztest/<subsystem>/`（**不是** `tests/`），用 pytest：

```bash
python -m pytest metagpt/ztest/{roles,loop,executor,think,context,skills,router,tasks,environment} -q
```

</content>
