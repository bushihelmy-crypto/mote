# AgentFrame

一个基于**组合式编排**的轻量级 Agent 框架。`Role`（角色/智能体）不再继承庞大的基类，而是把 `ThinkEngine`、`ToolExecutor`、`ContextManager`、`CommandChannel`、`LLMRouter`、`SkillManager` 等子系统按需组合进来，通过一个协议无关的 **ReAct 循环**（想 → 做）驱动任务执行。

> 本框架由 MetaGPT 重构而来，移除了 Pydantic 继承与巨型基类，强调「组合优于继承、依赖注入、协议无关」三大原则。完整设计细节见仓库根目录的 。

---

## 目录

- [设计理念](#设计理念)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [核心概念](#核心概念)
  - [Role：组合式编排器](#role组合式编排器)
  - [一次 run() 的全景流程](#一次-run-的全景流程)
  - [命令协议：XML vs Native](#命令协议xml-vs-native)
  - [工具系统](#工具系统)
  - [Router：LLM 路由](#routerllm-路由)
  - [Skills：技能系统](#skills技能系统)
  - [Environment：多 Agent 控制平面](#environment多-agent-控制平面)
- [扩展点速查表](#扩展点速查表)
- [依赖](#依赖)

---

## 设计理念

1. **组合优于继承**：`Role` 是纯编排器，所有子系统都是 lazy-init 的组合成员（`roles/role.py:88-96`）。
2. **依赖注入 + 窄接口**：下游组件只看到自己需要的「窄面」，从不反向持有 `Role`。工具只能拿到 `Role.tool_capabilities()` 白名单里的方法。
3. **协议无关的循环**：「想（think）→ 做（act）」主循环不关心底层是 XML 文本协议还是原生 tool-use，差异全部由 `CommandChannel` 策略吸收。
4. **抽象与契约分离**：`common/base/` 放需要被继承的抽象基类（ABC）；`common/interface/` 放用于鸭子类型的结构化 Protocol（零依赖叶子包）。

---

## 项目结构

```
metagpt/
├── common/          # 核心基础设施与共享工具
│   ├── base/        #   可继承的抽象基类（BaseRole / BaseLoop / CommandChannel / BaseThinkEngine 等）
│   ├── interface/   #   零依赖 Protocol 契约（LLMClient / MessageStore / BackgroundPool 等）
│   ├── schema/      #   数据模型（Message / AIMessage / UserMessage / ThinkResult / ToolResult 等）
│   ├── config/      #   配置模型（LLMConfig 等）
│   ├── const/       #   常量
│   ├── exception/   #   自定义异常
│   └── utils/       #   日志、YAML、token 计数、媒体处理等工具
├── roles/           # Role 类与 Schema/State
│   ├── role.py            #   Role：统一编排器
│   ├── role_schema.py     #   RoleSchema：静态配置（部署期）
│   ├── role_state.py      #   RoleState：可序列化运行快照
│   ├── context_provider/  #   ContextProvider：为每轮 ReAct 组装参数
│   └── agents/            #   预置角色
├── loop/            # ReAct 循环：观察 → 想 → 做 → 收尾（react_loop.py）
├── think/           # ThinkEngine：封装 LLM 调用、去重检查
├── executor/        # 工具执行引擎
│   ├── base_tool.py       #   BaseTool 抽象类（schema 自动生成 + 能力注入）
│   ├── tool_registry.py   #   @register_tool 注册表 + 自动发现
│   ├── tool_executor.py   #   ToolExecutor：把 LLM 的指令派发到工具实例
│   ├── tools/             #   内置工具（Read/Write/Edit/Bash/Glob/Grep/Notebook/Human/End/Sleep 等）
│   └── mcp/               #   MCP（Model Context Protocol）动态工具集成
├── parser/          # 协议适配器
│   ├── xml_channel.py     #   XmlCommandChannel：文本协议（XML 命令块）
│   └── native_channel.py  #   NativeToolChannel：原生 tool-use（JSON Schema）
├── context/         # ContextManager：会话历史存储 + 上下文压缩编排
│   ├── manager.py         #   两段式压缩（microcompact / autocompact）
│   ├── microcompact.py    #   无 LLM 的廉价折叠
│   └── autocompact.py     #   触发阈值后的 LLM 摘要重建
├── router/          # LLM 路由与选择
│   ├── router.py          #   LLMRouter：显式 / 任务映射 / 智能三种路由
│   ├── strategy.py        #   可插拔 RoutingStrategy
│   └── llm/               #   BaseLLM 实现、provider 注册、故障恢复
├── skills/          # 技能系统（SKILL.md 定义 + 注入）
│   └── builtin/           #   内置技能
├── tasks/           # BackgroundTaskPool：异步后台任务管理
├── memory/          # 记忆子系统（episodic / procedural / semantic）
├── environment/     # 多 Agent 控制平面
│   ├── control.py         #   AgentControl：会话级控制平面
│   ├── mailbox.py         #   agent 间消息投递
│   ├── scheduler.py       #   事件驱动 turn 调度
│   └── registry.py        #   agent 注册表 / 昵称池 / 总数上限
├── prompts/         # 提示词构建与模板（PromptBuilder）
└── ztest/           # 测试套件（结构镜像主目录）
```

---

## 快速开始

### 1. 创建一个最小的 Role

```python
from metagpt.roles.role import Role

# 仅指定名字，使用全部默认配置
role = Role(name="Alice")

# 转发 schema 字段：目标、可用工具、命令协议等
role = Role(
    name="Developer",
    goal="Write and run code",
    tools=["Read", "Write", "Bash"],
    command_protocol="native",   # "native"（推荐）或 "xml"
    max_react_loop=50,
)
```

`Role` 接受的关键参数（其余 `**schema_kwargs` 会被转发给 `RoleSchema`，见 `roles/role.py:60-69`）：

| 参数 | 说明 |
|---|---|
| `name` | 角色名（覆盖 schema 中的 name） |
| `context` | LLM 构建上下文（实际执行所需，见下文） |
| `config` | 外部注入的配置 |
| `role_schema` | 显式传入的 `RoleSchema`（静态配置） |
| `state` | 显式传入的 `RoleState`（可序列化运行快照） |

### 2. 配置 LLM 并运行

实际跑起来需要一个携带 LLM 配置的 `Context`：

```python
import asyncio
from metagpt.router.llm.context import Context
from metagpt.common.config.llm_config import LLMConfig, LLMType
from metagpt.roles.role import Role

# 构建 Context（默认从全局 Config 读取 llm 字段）
context = Context()

role = Role(name="Alice", goal="帮我读取文件", tools=["Read"], context=context)

async def main():
    msg = await role.run(with_message="读取 ./README.md 并总结要点")
    print(msg.content)

asyncio.run(main())
```

`LLMConfig` 的关键字段（见 `common/config/llm_config.py`）：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `api_key` | `"sk-"` | API 密钥，支持单个字符串或列表（失败时轮换） |
| `api_type` | `OPENAI` | provider 类型，见下方枚举 |
| `base_url` | `https://api.openai.com/v1` | 接口地址 |
| `model` | `None` | 模型名 / 部署 ID |
| `max_token` | `4096` | 最大输出 token |
| `temperature` | `0.0` | 采样温度 |
| `timeout` | `600` | 请求超时（秒） |
| `stream` | `False` | 是否流式 |
| `proxy` | `None` | 代理地址 |

支持的 `api_type`（`LLMType`）：`openai`、`fireworks`、`open_llm`、`moonshot`、`mistral`、`yi`、`open_router`、`deepseek`、`siliconflow`。

### 3. 序列化与恢复

`Role` 自身无需可序列化，序列化只需 dump `RoleSchema` + `RoleState`：

```python
data = role.dump()             # -> dict（含 role_schema / state）
restored = Role._from_dict(data)
```

---

## 核心概念

### Role：组合式编排器

`Role` 是整个框架的门面，本身几乎不含业务逻辑，只负责装配子系统并驱动一次 `run()`。

- **静态 vs 运行态分离**：`RoleSchema`（部署期配置）与 `RoleState`（运行快照）分开。
- **能力白名单是解耦关键**：工具想用 Role 的能力（如 `get_cwd`、`ask_human`）必须在 `tool_capabilities()` 中显式列出（`roles/role.py:360-378`），`bind()` 只注入这些，永不 `getattr(role, ...)`。
- **`active` 信号双重身份**：既是循环的迭代开关，又是工具→循环的「急停开关」——`End` 工具或 `ask_human("...stop")` 调 `deactivate()` 即可中断循环。

`RoleSchema` 常用字段（见 `roles/role_schema.py`）：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `name` / `profile` / `goal` | `"Zero"` / `"Role"` / `""` | 身份信息 |
| `command_protocol` | `"native"` | 命令协议：`"native"` 或 `"xml"` |
| `max_react_loop` | `50` | 单次 run 的最大循环步数 |
| `max_consecutive_react_limit` | `10` | 连续无进展上限 |
| `tools` | `[]` | 可用工具名列表 |
| `mcps` | `[]` | MCP server 列表 |
| `agents` | `[]` | 可委派的子 agent |
| `skills` | `[]` | 技能名列表 |
| `enable_memory` / `memory_k` | `True` / `30` | 记忆开关与保留条数 |
| `use_summary` | `True` | 会话结束是否总结 |
| `enable_router` | `False` | 是否启用智能路由 |

### 一次 run() 的全景流程

```
用户消息
  → Role.run() 入 msg_buffer
  → ReActLoop.run() 观察并写入 memory
  → [循环] ContextProvider.prepare() 组装请求（ContextManager 压缩历史）
  → Router 选模型 → ThinkEngine 调 LLM → ThinkResult
  → CommandChannel 解析出指令 → ToolExecutor 逐个执行 → 写回 memory
  → 终止条件满足 → 返回 AIMessage → publish 回环境
```

对应源码：
- 入口编排 `Role.run()` → `roles/role.py:543-576`
- 循环主体 `ReActLoop.run()` → `loop/react_loop.py:217-292`
- think 步 `_step_think()` → `loop/react_loop.py:127-144`
- act 步 `_step_act()` → `loop/react_loop.py:146-197`

**两种终止机制**：
- **XML 协议**：模型发出 `End` 指令 → `deactivate()` → 下一轮因 `is_active()` 为假而 break。
- **Native 协议**：模型回复纯文本、无 `tool_calls` → `channel.is_terminal()` 为真 → 直接返回。

### 命令协议：XML vs Native

`CommandChannel` 是吸收协议差异的策略接口，让 ReAct 循环对协议无感。`make_command_channel(protocol, provider=)` 负责分流；native 的 envelope（OpenAI vs Anthropic）在运行时由 `infer_native_tool_provider(llm_config)` 推断（模型名含 `claude` → anthropic，否则 openai）。

| 维度 | XmlCommandChannel | NativeToolChannel |
|---|---|---|
| 指令载体 | 响应文本中的 XML 块 | LLM 原生 `tool_calls`（JSON） |
| `tool_specs` | `None`（不走 specs） | `executor.get_native_tool_specs(provider)` |
| `is_terminal` | 恒 False（靠 End 工具终止） | `tool_calls == []`（纯文本回复即终止） |
| 参数类型 | **全部当字符串**（仅标量） | 结构化 JSON Schema（支持嵌套模型） |

> ⚠️ XML 协议不携带参数类型，结构化参数（list/dict/model）只在 native 通道正确工作（`executor/base_tool.py:40-45`）。设计带结构化参数的工具时需限定 native。

### 工具系统

定义一个工具：继承 `BaseTool`，实现 `call(**kwargs)`，用 `@register_tool` 装饰即可自动发现。schema 从 `call()` 的签名 + docstring 自动生成。

```python
from typing import Callable, ClassVar
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tool_result import ToolError


@register_tool
class Bash(BaseTool):
    """Run a bash command in the current working directory and return its output."""

    name = "Bash"
    aliases = ["Bash.run", "bash"]
    max_result_size_chars: ClassVar[int] = 30_000
    description = "Execute a bash command. State (cwd) persists across calls within a session."
    requires = ("get_cwd", "set_cwd")          # 需要 Role 发布的窄能力

    # bind() 时由 Role 注入，只拿这两个 cwd 访问器，永不触及 RoleState/memory
    get_cwd: Callable[[], str]
    set_cwd: Callable[[str], None]

    async def call(self, *, command: str, timeout: float = 300.0) -> str:
        """Execute a bash command in the session's current working directory.

        Args:
            command: The bash command to execute.
            timeout: Maximum seconds to wait for the command (default 300).
        """
        if not command or not command.strip():
            raise ToolError("Error: 'command' argument is required.")
        cwd = self.get_cwd()
        # ... 执行并通过 set_cwd 持久化 cwd ...
        return output
```

关键点：
- **类属性**：`name`（主名/查找键）、`aliases`（别名）、`description`（留空则取 docstring 首行）、`requires`（需注入的 Role 能力名）、`max_result_size_chars`（输出上限）。
- **能力注入**：`bind(session_id, role)` 校验 `requires` 中每个名字都在 `role.tool_capabilities()` 白名单里，否则抛 `AttributeError`（`executor/base_tool.py:77-96`）。
- **大输出落盘**：超过 `max_result_size_chars` 的文本结果写盘并替换为预览；带媒体（图片/PDF）的结果原样发给模型。
- **失败快停但全记录**：首个失败后停止真正执行，但仍为剩余指令补记 `[SKIPPED]` 结果（native 要求每个 `tool_call` 必须配对 `tool_result`）。

内置工具：`Read`、`Write`、`Edit`、`Bash`、`Glob`、`Grep`、`NotebookEdit`、`Human`、`End`、`Sleep`、`AgentTool` 等（见 `executor/tools/`）。

### Router：LLM 路由

`LLMRouter` 取代了旧的 `LLM()` 工厂，统一三种选模型方式（`router/router.py`）：

```python
from metagpt.router.router import get_router
from metagpt.common.config.llm_config import LLMConfig

router = get_router()

# ① 显式路由：直接给 LLMConfig 或模型名（等价旧 LLM() 工厂）
llm = router.route(llm_config=LLMConfig(api_key="sk-...", model="gpt-4o"))

# ② 任务映射路由：声明任务类型，由 task_map 选模型
llm = router.route_for_task("compression")   # 上下文压缩用更便宜的模型
llm = router.route_for_task("summary")        # 会话总结

# ③ 智能路由：把请求信号交给可插拔 Strategy 选档
# decision = await router.aroute(routing_request)
```

- **任务映射**优势：新增任务路由只需加一行映射 + Config 加字段，无需写分支。
- **故障恢复**：provider 层包了 COMPRESS / ROTATE_CREDENTIAL / FALLBACK / SHRINK_IMAGE 等注入式恢复回调；`api_key` 为列表时失败自动轮换。

### Skills：技能系统

技能是 `SKILL.md` 文件，解析为 `SkillDefinition`，由 `SkillManager` 加载、部署到工作区、并把指令注入到提示词中。

```python
role = Role(
    name="Developer",
    skills=["code-generation", "documentation"],   # 在 RoleSchema 上声明
)
# SkillManager 首次访问时惰性初始化：role.skill_manager
```

`SkillDefinition` 字段（见 `skills/skill_definition.py`）：`name`、`description`、`always_apply`、`globs`、`roles`、`instructions`、`metadata` 等。

### Environment：多 Agent 控制平面

`environment/` 提供会话级控制平面 `AgentControl`，由 Registry（agent 树/昵称池/上限）、Limiter（并发上限）、Residency（LRU 卸载到磁盘）、Scheduler（事件驱动调度）、Store（磁盘序列化）五个原语组合。

两种投递模式（`environment/mailbox.py`）：
- `TRIGGER_TURN`：入队后立即唤醒目标 agent 跑一个 turn。
- `QUEUE_ONLY`：只入队，目标在下一个 turn 边界才看到——用于子 agent 完成通知，避免打断父 agent 当前 turn。

---

## 扩展点速查表

| 想做的扩展 | 怎么做 | 关键文件 |
|---|---|---|
| **加一个工具** | 继承 `BaseTool`，写 `call(**kwargs)`，`@register_tool` 装饰，schema 自动生成 | `executor/base_tool.py`、`executor/tool_registry.py` |
| **工具需要 Role 能力** | 工具上声明 `requires=("get_cwd", ...)`，并在 `Role.tool_capabilities()` 发布 | `executor/base_tool.py:77`、`roles/role.py:360` |
| **接一个新 LLM provider** | 继承 `BaseLLM` 实现抽象方法，`@register_provider([...])` | `router/llm/base_llm.py`、`router/llm/llm_provider_registry.py` |
| **加一种路由策略** | 实现 `RoutingStrategy.select()`，`router.set_strategy()` 注入 | `router/strategy.py`、`router/router.py` |
| **加一个任务路由** | 在 task_map 加一行 + Config 加 `LLMConfig` 字段 | `router/router.py` |
| **加一种命令协议** | 实现 `CommandChannel` 抽象方法，在 `make_command_channel` 分流 | `common/base/command_channel.py`、`parser/native_channel.py` |
| **换 ReAct 循环策略** | 实现 `BaseLoop`，在 `Role._make_loop()` 选择 | `common/base/loop.py`、`roles/role.py` |
| **加一个后台任务工具** | 工具返回 `BgTaskResult`，由 `BackgroundTaskPool` 接管 | `tasks/pool.py`、`executor/tool_executor.py` |
| **多 agent 编排** | 用 `AgentControl` 注册 runtime、`send_input` 投递 | `environment/control.py` |
