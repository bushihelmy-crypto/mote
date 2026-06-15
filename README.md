# AgentFrame (metagpt)

一个面向编码与通用任务的 **Agent 运行框架**（Claude Code / Codex 风格）。核心是一个统一的 `Role`：它在一个 **ReAct（think → act）循环**里调用 LLM，解析模型产出的命令，执行工具，并把结果写回上下文，直到任务结束。

> 本目录（`metagpt/` 包）即整个框架。下文描述的所有路径都相对于本目录。

---

## 能做什么

- **交互式 CLI**：`python -m metagpt.cli` 打开一个 REPL，和一个 agent 对话、让它读写文件、执行命令、跑代码。
- **工具执行**：内置文件读写/编辑、`Bash`（一次性子进程）、`Terminal`（持久 PTY 终端）、`Jupyter`（持久 Python kernel）、`Grep`/`Glob`、子 agent 派生（`Agent`）等，并支持 **MCP**（Model Context Protocol）外接工具。
- **两种命令协议**：原生工具调用（`native`，OpenAI/Anthropic tool-use）或 XML 文本协议（`xml`）。
- **多模型路由**：固定模型，或按任务/复杂度智能选择模型；原生支持 OpenAI 兼容协议与 Anthropic Messages API。
- **上下文管理**：token 预算跟踪 + 廉价的工具结果折叠（microcompact）+ LLM 摘要重建（autocompact）。
- **会话持久化**：追加式 JSONL rollout 日志，支持 **resume / fork / list**，以及文件改动的内容寻址快照（可 diff / 回滚）。
- **多 agent 环境**：控制面（control plane）+ 邮箱 + 事件驱动调度 + LRU 驻留（idle agent 落盘、按需复活）+ 定时任务 + 文件监听。
- **权限系统**：模式（mode）+ 规则（rules）+ 沙箱（sandbox）三轴，deny/ask 不可被 bypass 绕过，命令安全分类器。
- **生命周期 Hook**：PreToolUse / PostToolUse / UserPromptSubmit / SessionStart / Stop / PreCompact / PostCompact / FileChanged。
- **可观测性**：loguru 结构化日志（`@log_class` 自动埋点 + trace-id）、Langfuse LLM tracing、成本/token 统计。

---

## 快速开始

### 1. 配置 LLM

最小配置只需要 `llm`。编辑 `config.yaml`（PROJECT 层），或在更高优先级层覆盖：

```yaml
llm:
  api_key: "sk-YOUR_API_KEY"
  api_type: "openai"            # openai | anthropic | fireworks | open_llm | moonshot | deepseek | ...
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o"
```

> `base_url` 含 `anthropic.com` 或 `api_type: anthropic` 时，自动走原生 Anthropic Messages API。

配置加载优先级（低 → 高，高者覆盖低者）：

```
defaults < system < user < project < workdir < profile < env < cli < programmatic < managed
```

- `project`：本目录 `config.yaml`
- `user`：`~/.agentframe/config.yaml`（兼容 `~/.metagpt/config2.yaml`）
- `workdir`：`<cwd>/.agentframe/config.yaml`（**不受信任**，凭据字段会被剥离）
- `env`：环境变量 `AGENTFRAME_*` / `METAGPT_*`（如 `AGENTFRAME_LLM__BASE_URL=...`，`__` 分隔层级）
- `cli`：运行时 `-c key=value` 覆盖
- `managed`：`/etc/agentframe/managed.config.yaml`（管理员策略，覆盖一切）

### 2. 启动 REPL

```bash
python -m metagpt.cli                       # 默认 agent
python -m metagpt.cli --model claude-sonnet-4-8
python -m metagpt.cli --tools Read,Write,Edit,Bash,Glob,Grep
python -m metagpt.cli --cwd /path/to/project --name Coder
```

REPL 内：

- 直接输入 = 一个对话回合（turn）。
- `Ctrl+C` 一次中断当前回合；在空提示符下连按两次退出。
- 以 `/` 开头的行是斜杠命令：

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助 |
| `/exit`, `/quit` | 退出 REPL |
| `/agents` | 列出本会话控制面里的 agent |
| `/agent <ref>`, `/switch <ref>` | 切换活动 agent（序号 / session-id / 名字） |
| `/new [name]` | 新建一个 agent 并切换 |
| `/fork` | 把当前会话 fork 成一个新 agent 并切换 |
| `/sessions`, `/list` | 列出可恢复的会话（最新优先） |
| `/resume <ref>` | 恢复一个会话（`/sessions` 序号 或 session-id） |

### 3. 以库的方式使用

```python
from metagpt.roles import Role, RoleSchema, RoleState

schema = RoleSchema(name="Coder", tools=["Read", "Write", "Edit", "Bash"])
role = Role(role_schema=schema, state=RoleState())
reply = await role.run(with_message="帮我在 main.py 里加一个 hello 函数")
```

---

## 执行路径速览

```
REPL (cli/repl.py)
  └─ AgentControl (environment/control.py)          # 控制面：投递输入、触发回合
       └─ AgentRuntime (environment/runtime.py)      # 每回合包一次 Role.run
            └─ Role.run (roles/role.py)              # 编排：hook、trace、装配 loop
                 └─ ReActLoop (loop/react_loop.py)   # observe → think → act 循环
                      ├─ observe   : 从消息缓冲拉取、过滤、写入 ContextManager
                      ├─ think     : ContextProvider 组装请求 → Router 选 LLM
                      │              → ThinkEngine 调用 LLM（流式）
                      └─ act       : CommandChannel 解析命令 → ToolExecutor 执行
```

支撑子系统：`context/`（消息存储 + 压缩）、`router/`（模型路由 + LLM 客户端 + 成本）、`session/`（持久化）、`parser/`（命令协议）、`think/`（提示词组装 + LLM 调用）、`executor/`（工具 + 权限 + MCP）。

详见 [`ARCHITECTURE.md`](./ARCHITECTURE.md)。给贡献者/agent 的工作约定见 [`AGENTS.md`](./AGENTS.md)。

---

## 目录结构

```
metagpt/
├── cli/            交互式 REPL 入口（__main__、repl、commands、render）
├── common/         叶子基础层：base/ config/ schema/ interface(Protocols)/ hook/
│                   prompt/ logs/ exception/ const/ utils/ git_state/ observability/
├── roles/          Role（统一编排类）+ RoleSchema/RoleState + context_provider/ + lsp/ + agents/
├── loop/           ReActLoop（think→act 循环）
├── think/          ThinkEngine（调用 LLM）+ PromptBuilder（提示词组装）
├── context/        ContextManager（消息存储 + 压缩）+ autocompact/microcompact/token_budget
│                   + turn_context/（per-turn ephemeral 注入：git/token/bg/lsp）
├── executor/       ToolExecutor + tools/ + permission/ + mcp/ + dependency/
├── parser/         命令协议 channel：xml_channel / native_channel
├── router/         LLMRouter + llm/（openai/anthropic 客户端）+ cost/ + ml/ + oauth/ + 路由策略
├── session/        会话持久化：events/log/replay/listing/fork/snapshot/history
├── environment/    多 agent：runtime/control/registry/mailbox/scheduler/residency/store
│                   + scheduling/（cron）+ watching/（文件监听）+ mgx/
├── skills/         技能定义（SKILL.md）+ 加载/注入
├── tasks/          后台任务池（BackgroundTaskPool）+ 进度 attachment
├── memory/         记忆模块（episodic / procedural / semantic）
└── config.yaml     PROJECT 层配置模板（含中文注释）
```

---

## 测试

测试位于 `metagpt/ztest/<subsystem>/`（用 pytest 运行）：

```bash
python -m pytest metagpt/ztest/{roles,loop,executor,think,context,skills,router,tasks,environment} -q
```

本项目已通过集成测试并成功跑通。
