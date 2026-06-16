# AgentFrame 架构文档

> AgentFrame（`metagpt.*` 包）是一个**组合式、事件驱动、分层解耦**的 agent 运行框架。它把一个 agent 的运行拆成「想（think）/ 做（act）」对偶的 ReAct 循环，外接多 agent 运行时、会话持久化、统一 LLM 路由与权限沙箱，全部架在一个零反向依赖的 `common` 基础层之上。

## 0. 全局总览

### 0.1 架构全景图

自上而下是依赖方向（高层 import 低层），横向展开的是一次 turn 内 **think / act** 的对偶分工。跨层能力一律经 `common/interface/` 的 Protocol 做**依赖倒置**（底层定义接口面、高层注入实现），故同层之间互不直接耦合。

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  cli  ——  入口 / 交互式 REPL（构造 Role、逐行驱动 turn）            ※不在本文详述   │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │ 构造 + 驱动
┌────────────────────────────────────────▼───────────────────────────────────────┐
│  environment  ——  多 agent 运行时                                                  │
│  控制面 AgentControl · 事件调度器 · 层级 AgentPath · 邮箱 · LRU 驻留 · cron · 文件监视  │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │ 包裹 Role 并调度其 turn
┌────────────────────────────────────────▼───────────────────────────────────────┐
│  roles  ——  Role 编排核心（组合优于继承）                                           │
│  RoleSchema(静态配置) + RoleState(可序列化运行态) + RoleComponents(惰性装配子系统)     │
│  └ context_provider(请求装配管线)   └ lsp(诊断回流，opt-in)                          │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │ 把循环逻辑委托给 loop（role-agnostic）
┌────────────────────────────────────────▼───────────────────────────────────────┐
│  loop  ——  ReAct 主循环：  observe → think → act → finish                          │
└──────────┬─────────────────────────────────────────────────────────┬─────────────┘
  think 侧  │                                                  act 侧  │
┌──────────▼────────────┐   ┌──────────────────────┐   ┌────────────▼──────────────┐
│ think                  │   │ parser               │   │ executor                   │
│ 组装 system/user prompt │←─▶│ 命令协议通道（翻译层）  │◀─▶│ 工具执行引擎（单咽喉派发）    │
│ + 后台调 LLM            │   │ XML  ⇄  native IR     │   │ run_command:校验→权限→恢复→限流│
│ → ThinkResult          │   │ command_guide/hint    │   │ tools·permission·tasks·mcp │
└──────────┬────────────┘   └──────────────────────┘   └────────────┬──────────────┘
           │ 历史 / token 预算                                        │ 选模型 / 计费
┌──────────▼────────────┐                              ┌─────────────▼──────────────┐
│ context                │                              │ router                      │
│ 历史 CRUD + 两级压缩     │                              │ LLM 选型 + 多 provider 接入   │
│ + skills + per-turn 注入│                              │ llm · ml · oauth · cost      │
└──────────┬────────────┘                              └─────────────┬──────────────┘
           │                                                          │
┌──────────▼──────────────────────────────────────────────────────────▼────────────┐
│  session  ——  会话持久化（追加式 rollout.jsonl 为崩溃安全真相源）                       │
│  events · log · replay · listing · fork · snapshot(blob/git) · history · subscribers │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │ 所有上层一律向下依赖
┌────────────────────────────────────────▼───────────────────────────────────────┐
│  common  ——  框架基础层（叶子，永不反向 import 上层）                                 │
│  base(ABC) · interface(Protocol⇡依赖倒置) · schema · const · config · exception ·    │
│  events · hook · logs · prompt · observability · scheduling · utils                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

> `memory/`（程序记忆/语义记忆/情景记忆）当前为占位/待实现，未进入主链路；本文末尾单独列出其包结构。

### 0.2 包索引

| 包 | 层 | 一句话职责 | 关键入口 |
|------|------|-----------|---------|
| `common` | 0 基础 | 抽象基类 / 跨层 Protocol / 数据模型 / 配置 / 异常 / 事件 / 钩子 / 日志 / 提示词 / 工具函数 | `common/interface/` · `common/schema/messages.py` |
| `context` | 1 | LLM 看到什么：消息 CRUD + 两级压缩 + 技能注入 + per-turn 易变上下文 | `context/manager.py` · `context/turn_context/` |
| `executor` | 1 | 行动侧引擎：工具单咽喉派发 + 权限沙箱 + 后台任务 + MCP | `executor/tool_executor.py` · `executor/permission/` |
| `router` | 1 | LLM 模型选型 + 多 provider 抽象 + 成本 / OAuth / 恢复 | `router/router.py` · `router/llm/base_llm.py` |
| `session` | 1 | 会话历史的崩溃安全持久化（rollout/replay/snapshot/fork） | `session/log.py` · `session/replay.py` |
| `parser` | 2 | 命令协议通道：XML ⇄ native tool-use 统一成命令 IR | `parser/factory.py` |
| `think` | 2 | 思考侧：组装 prompt + 后台调 LLM → `ThinkResult` | `think/think_engine.py` · `think/prompts/` |
| `loop` | 2 | ReAct 主循环（observe/think/act/finish） | `loop/react_loop.py` |
| `roles` | 3 | Role 编排核心（schema + state + 惰性组件装配） | `roles/role.py` · `roles/role_components.py` |
| `environment` | 4 | 多 agent 控制平面 + 调度 + 驻留 + cron + 文件监视 | `environment/control.py` |
| `cli` | 5 入口 | 交互式 REPL / 命令行入口（**不在本架构文档详述**） | `cli/repl.py` |
| `memory` | — | 程序记忆/语义记忆/情景记忆（待实现） | `memory` |

### 0.3 分层硬约束（依赖单向向下）

```
common  ◀──  context / executor / router / session  ◀──  parser / think / loop  ◀──  roles  ◀──  environment  ◀──  cli
```

- `common` 是叶子，**只依赖标准库 + 第三方**（pydantic / loguru / openai / anthropic …），永不反向 import 任何上层。
- 低层要用高层能力 → 先在 `common/interface/` 定义 `@runtime_checkable` Protocol，由高层在装配期注入实例，**不直接 import**（依赖倒置）。典型：`context↛session`（经 `SessionRecorder`）、`executor↛session`（经 `FileSnapshotStore`）、`executor↛roles`（经 `HookRunner`/`LspNotifier`）、`turn_context↛tasks`（经 `EphemeralContextSource`）。
- `environment → session` 可在加载期直接 import（无环）；`environment → roles` 必须惰性 import 打破环。
- `session` 虽被 `roles`/`environment` 消费，但自身只依赖 `common`，故归在第 1 层。

---

## `metagpt.common` —— 框架基础层

`common` 是整个 AgentFrame 的**最底层基础包**。它的核心设计约束是单向依赖：

> `common` 只依赖标准库 + 第三方库（pydantic / loguru / openai / anthropic 等），**永不**反向 import 任何上层包（`roles` / `executor` / `context` / `router` / `session` / `environment` / `tasks`）。

所有跨层解耦都通过 `common/interface/` 中的 **Protocol（结构化协议）** 完成：底层定义"接口面"，上层注入"具体实现"。这使得 `common` 可以被任意层安全 import，而上层之间互不耦合。

### 子包总览

| 子包 | 职责 | 一句话定位 |
|------|------|-----------|
| `base/` | 抽象基类 / ABC / 元类 | 框架的扩展点与生命周期契约 |
| `interface/` | runtime_checkable Protocol | 跨层解耦的"接口面"（依赖倒置） |
| `schema/` | pydantic 数据模型 | 全框架的领域数据结构（Message 等） |
| `const/` | 常量 | 路径、token 预算、限额等单一真相源 |
| `config/` | 分层配置中心 | 多源合并 + 类型化 Config |
| `exception/` | 异常体系 | 分层异常 + 错误码 + 恢复策略 |
| `events/` | 事件总线 | 统一的发布/订阅事件脊柱 |
| `hook/` | 钩子系统 | Python 回调 + 外部命令的生命周期拦截 |
| `logs/` | 日志 | loguru + trace-id + 自动装饰 + 流式 |
| `prompt/` | 提示词模板 | 所有 model-facing / system 文本的单一来源 |
| `observability/` | 可观测性 | Langfuse 追踪集成（默认关，懒加载） |
| `scheduling/` | 调度原语 | 通用周期轮询循环 |
| `utils/` | 工具函数 | token 计数、解析修复、git 状态、序列化等 |

---

## 1. `common/base/` —— 抽象基类层

定义框架的核心扩展点。`__init__.py` 刻意按"零依赖优先"顺序导入以避免部分初始化，导出 8 个核心抽象。

### 1.1 `singleton.py` —— 单例元类
- **`Singleton(abc.ABCMeta, type)`**：双继承 `ABCMeta`+`type`，允许抽象方法与单例行为共存。`__call__` 拦截实例化，按类缓存到 `_instances`。线程不安全（适用于初始化期单例，如 `TERMINAL`、`KERNELS`、各 provider 注册表）。

### 1.2 `role.py` —— 多态序列化注册表
- **`BaseRole`**（纯基类，无 ABC，便于与其他元类组合）：
  - `_ROLE_REGISTRY: dict[str, type]` + `__init_subclass__` 自动注册（key = `module.ClassName`）。
  - `dump()`（子类实现）/ `load(data)`（查注册表路由到子类 `_from_dict`）。
  - 缺类名 → ValueError；未知类 → TypeError。

### 1.3 `agent.py` —— Agent 自描述 mixin
- **`BaseAgent`**：ClassVar `agent_name`/`aliases`/`description`（fallback 到 docstring 首行）+ `get_schema()`。`agent_name`（而非 `name`）刻意避免遮蔽 `Role.name`。配 `@register_agent` 使用。

### 1.4 `command_channel.py` —— 协议策略 ABC
- **`CommandChannel(ABC)`**：封装"协议形态的 LLM 交互"，把协议逻辑挡在 loop 之外。
  - 抽象方法：`output_format()`、`tool_specs(executor)`（native specs 或 None）、`iter_commands(...)`（解析为统一 IR `{command_name,args,id,status,error_msg}`）、`record_turn(...)`。
  - 默认/钩子方法：`command_guide()`、`command_hint()`（XML vs native 协议门控）、`turn_signature()`、`is_terminal()`。
  - 两个具体实现：**XML channel**（解析 `End` 命令、文本里追踪 tool_calls）/ **Native channel**（读结构化 `tool_calls`，空则停）。协议区分基于**传输层 wire 协议**（OpenAI vs Anthropic），不是 model 名。

### 1.5 `loop.py` —— react 循环策略
- **`LoopContext`**（dataclass）：role-agnostic 的不可变循环配置（`max_react_loop`、`memory_k`、`msg_buffer`、`watch`、`enable_memory` 等）。
- **`BaseLoop(ABC)`**：单一抽象方法 `async run() -> Message | None`。只接收可复用组件（think_engine / command_channel / executor / context_provider），**绝不接收 Role**——Role 退化为装配器 + 消息发布者，loop 拥有循环逻辑。具体实现 `ReActLoop`。

### 1.6 `think_engine.py` —— LLM 调用编排
- **`BaseThinkEngine(ABC)`**：`llm:LLMClient` / `memory:MessageStore` / `result:ThinkResult`。抽象 `start(...)`（后台启动一轮）/ `join()` / `done`。`tool_specs=None` 在 XML 文本通道与 native tool-use 通道间切换。调用方只通过 `result` 单一属性读结果。

### 1.7 `postprocess_plugin.py` —— LLM 输出修复
- **`BasePostProcessPlugin`**：编排 `run_repair_llm_output`（大小写修复 → 抽内容 → 修 JSON → 解析重试），各步骤可子类 override，按模型定制。

---

## 2. `common/interface/` —— 结构化协议层（依赖倒置核心）

**LEAF 包**：只 import `typing` + `TYPE_CHECKING` 下的 schema 类型，可在任何地方安全 import。所有 Protocol 都 `@runtime_checkable`，靠鸭子类型而非继承耦合。模式统一：**Protocol 定义在此底层，具体实现在高层，由 Role/装配点注入**。

| Protocol | 方法面 | 实现方（注入） | 消费方（解耦） |
|----------|--------|---------------|---------------|
| `MessageStore` | `get/add/add_batch/delete` | `ContextManager` | think_engine、loop |
| `RequestAssembler` | `prepare_request(...)` | `ContextManager` | `ContextProvider` |
| `LLMClient` | `model` / `aask` / `aask_tool` | `BaseLLM` 子类 | think_engine、loop |
| `BackgroundPool` | `has_pending/pending_count/wait_any/wait_for_completion` | `BackgroundTaskPool` | loop |
| `MessageActivity` | `wait_for_message()` | `MessageQueue` | bg pool、`Role.wait_interruptible` |
| `HookRunner` | `fire(event,payload,*,permission_mode)` | `HookManager` | ToolExecutor、ContextManager、Role |
| `FileSnapshotStore` | `snapshot(full_path,*,tool)` | `session.FileSnapshotRecorder` | executor 文件改写工具（隔离 executor↛session） |
| `EphemeralContextSource` | `name`/`priority`/`render(*,cwd)` | Git/Token/Lsp/BgTask Source | `TurnContextBus`（隔离 context↛tasks/roles） |
| `EventSubscriber` | `priority`/`handle`/`handle_sync` | 各订阅者 | `EventBus` |

> 关键作用：`ContextManager` 同时满足 `MessageStore`（存储面）与 `RequestAssembler`（请求构建面），接口隔离让不同消费方只看到对象的不同侧面，无法触及其编排逻辑（如 `manage_history`）。

---

## 3. `common/schema/` —— 数据模型层

纯数据 pydantic 模型 / dataclass。`__init__.py` 用 **lazy `__getattr__`** 按需导入子模块，打破循环依赖。

### 3.1 `messages.py` —— 核心 IR：`Message`
全框架 agent-LLM 对话的通用交换格式。
- **`Message`** 字段：`id`(UUID)、`timestamp`、`content`、`instruct_content`(结构化输出)、`role`、`cause_by`(CauseBy 标签)、`sent_from`、`send_to`(默认广播)、`metadata`(可扩展袋子：tool_calls / tool_call_id / agent 名 / 路由)。
- 关键方法：`to_dict()`(供 LLM API，处理 OpenAI tool_calls 信封)、`dump()`/`load()`(JSON 往返，容错)、`rag_key()`、`parse_resources(llm,...)`、`is_user/ai/tool_message()`。
- 便捷子类：`UserMessage`/`SystemMessage`/`AIMessage`(带 `tool_calls`、`with_agent()`)/`ToolMessage`(需 `tool_call_id`，映射 OpenAI `role=tool` 与 Anthropic `tool_result`)。
- `LLMCallContext`：记录最后一轮喂给 LLM 的精确消息序列（审计用）。

### 3.2 `think.py` —— `ThinkResult`
`content` + `tool_calls`(None=XML 通道)。属性 `is_native`(`tool_calls is not None`) / `is_empty`。纯结果类型，不含 Message/role 语义。

### 3.3 `context.py` —— 上下文管理配置与状态
- `ContextManagerConfig`：microcompact（折叠旧 tool 结果）+ autocompact（摘要重建历史）+ 请求级压缩三组旋钮。
- `TokenState`(frozen)：token 预算快照（`token_count`/`effective_window`/`percent_left`/`above_warning`/`above_autocompact`/`at_blocking_limit` 等）。
- `MicrocompactResult` / `AutocompactResult`：两种压缩策略的产出（含 `changed`、`summary`、token 前后量、失败计数）。

### 3.4 其余数据模型
- `serialization.py` `BaseSerialization`：用 `@model_serializer(wrap)` + `@model_validator(wrap)` 给 pydantic v2 加多态序列化（dump 时追加 `__module_class_name`，load 时查注册表实例化正确子类）。
- `document.py`：`CauseBy`(枚举标签，替代旧 Action marker)、`Document`/`Documents`、`SerializationMixin`、`Resource`。
- `queue.py`：`MessageQueue`(优先级邮箱，`MessagePriority` NOW/NEXT/LATER + `wait_for_message` 信号)、`LongTermMemoryItem`。
- `askuser.py`：`AskUserQuestionInput`(1-4 问) / `AskUserQuestionItem`(2-4 选项，chip ≤12 字符)，镜像 Claude Code Zod schema。
- 策略声明类（均在 RoleSchema 上声明，引擎在上层）：`PermissionConfig`/`SandboxConfig`、`HookConfig`、`LspConfig`、`FileWatchConfig`、`ToolResultLimitConfig`。
- `permission_types.py`：**零依赖**权限决策类型（Literal `PermissionMode`/`Behavior`/`GrantScope`/`RuleSource`/`RiskLevel` + `PermissionDecision`/`PermissionRule` dataclass），任何层可安全 import。
- `node_status.py` `BgStatus`：后台任务/图节点统一状态枚举。
- `env.py`：RL 风格 `BaseEnvironment`(`reset/observe/step` Gym 签名) 模板。

---

## 4. `common/const/` —— 常量层

按关注点拆分的单一真相源，全部从 `__init__.py` 再导出。

| 文件 | 内容 |
|------|------|
| `paths.py` | 文件系统路径根：`METAGPT_ROOT`(env 或 .git 上溯)、`SOURCE_ROOT`、`DEFAULT_WORKSPACE_ROOT`、`CONFIG_ROOT`、`SERDESER_PATH`、上下文协议文件名、各输出仓目录。含 `get_metagpt_root()`/`set_default_workspace_root()` |
| `llm.py` | `GENERAL_FUNCTION_SCHEMA`(execute 函数 schema)、`GENERAL_TOOL_CHOICE`、`MULTI_MODAL_MODELS`(支持图像的模型) |
| `message.py` | 消息路由键(`MESSAGE_ROUTE_*`)、元数据键(`AGENT`/`IMAGES`/`PDFS`/`TOOL_CALLS`/`TOOL_CALL_ID`) |
| `context.py` | 压缩阈值（移植自 Claude Code）：microcompact 触发/保留、autocompact buffer/keep-tail、`MODEL_CONTEXT_WINDOW_DEFAULT=200_000`、连续失败上限 |
| `tasks.py` | 后台任务旋钮：输出上限(5GB)、超时(600s)、并发(10)、stall 检测阈值 |
| `tools.py` | 工具限额：文件大小帽、Read 行数/行长、Grep 排除 VCS 目录/超时、Glob 上限、`ERROR_PREFIX` |
| `misc.py` | 杂项：MEM_TTL、默认语言/token、超时常量、UML 关系常量等（含历史遗留） |

---

## 5. `common/config/` —— 分层配置中心

多源配置收集 → 智能合并 → pydantic 校验 → 类型化 `Config`。设计对齐 codex 的 `ConfigLayerStack → Config` 分离。

### 5.1 9 层优先级栈（低 → 高）
```
DEFAULT(0) → SYSTEM(10,/etc) → USER(20,~/.agentframe|~/.metagpt 兼容)
→ PROJECT(30,metagpt/config.yaml 受信) → WORKDIR(35,<cwd>/.agentframe 不受信→剥离凭据)
→ PROFILE(40,命名覆盖) → ENV(50,AGENTFRAME_/METAGPT_) → CLI_FLAG(60,-c key=value)
→ PROGRAMMATIC(70,代码注入) → MANAGED(80,/etc 管理策略，锁死)
```
**合并语义**：dict 深合并（高层覆盖）、list 并集去重（低层在前）、scalar 高层胜。

### 5.2 关键文件
- `loader.py`：`load_config(...)` 主入口 + `build_layer_stack`(发现文件→YAML→剥离 WORKDIR 凭据→env/cli/programmatic 层) + `_build_config`(深合并→解析 api_key→校验)。`(cwd,profile)` 缓存，自定义入参时绕过。
- `layers.py`：`ConfigLayer`/`ConfigLayerStack`(`sorted_layers`/`effective`/`provenance`)、`deep_merge`、`strip_sensitive`(剥离 `CREDENTIAL_DENYLIST`：api_key/base_url/oauth/model_providers/api_key_helper)。
- `sources.py`：`ConfigSource`(IntEnum，值=优先级，`.trusted`) + `discover_source_files()`。
- `env.py`：`build_env_layer`，`__`=嵌套、`_`=键内字符，值经 YAML 解析。
- `overrides.py`：`parse_cli_overrides`(`-c a.b.c=value`) + `ConfigOverrides`(强类型袋：model/api_key/base_url/proxy/enable_router + extra 逃生口)。
- `meta_config.py`：根 `Config(YamlModel)`，字段 `llm`(必填)/`compress_llm`/`summary_llm`/`enable_router`/`exp_pool`/`role_zero`/`mcp`/`sentry`/`langfuse`。validator 让 task-llm 继承主 llm 凭据 + 激活 langfuse。`Config.default()` 类方法。
- `secrets.py`：`resolve_api_key`(占位 key `{"","sk-","YOUR_API_KEY"}` 时跑 `api_key_helper` shell 命令，300s TTL 缓存，best-effort)。helper 在 denylist 中 → WORKDIR 无法注入 RCE。
- `diagnostics.py`：`unknown_key_paths`(严格模式校验未知键) + `format_report`(层栈+provenance+未知键，凭据脱敏 `***`) + CLI `python -m ...config.diagnostics --strict`。
- `watcher.py`：`ConfigWatcher`，mtime 轮询（无 watchdog 依赖），变更→`load_config(reload=True)`→`on_reload` 回调。

### 5.3 `config/config/` 子模型
`llm_config.py`(`LLMConfig` + `LLMType` 枚举：ANTHROPIC 走原生客户端，其余走 OpenAI 兼容；provider preset 懒导 catalog 避免 common↛router 环) / `compress_msg_config.py`(`CompressType`) / `exp_pool_config.py` / `mcp_config.py` / `oauth_config.py`(`OAuthProviderConfig` + GrantType/StoreBackend) / `role_zero_config.py`(记忆+技能) / `langfuse_config.py` / `sentry_config.py`。

### 5.4 安全模型
WORKDIR 为唯一不受信源 → 进栈前剥离凭据键；`api_key_helper` 在 denylist → 无法被仓库本地配置注入任意命令。

---

## 6. `common/exception/` —— 异常体系

### 6.1 层次
- 根 **`MetaGPTError`**：`message`/`code`(ErrorCode)/`cause`/`context` + ClassVar `retryable`/`default_recovery`。`to_dict()` 序列化 + 支持 pickle(`_SanitizedCause` 兜底不可序列化 cause)。
- Marker mixin：**`RetryableError`**(retryable=True) / **`NonRetryableError`**(False) —— 通过 MRO 驱动重试决策。
- 分域：`llm.py`(LLMError 树，含 `ContextWindowExceededError`→COMPRESS、`LLMAuthenticationError`→ROTATE_CREDENTIAL 等带恢复动作)、`tool.py`、`router.py`、`graph.py`、`oauth.py`、`config.py`、`agent.py`、`environment.py`(agent 控制面)、`resource.py`(`NoMoneyException`)。

### 6.2 `codes.py`
- `ErrorCode`(StrEnum，稳定字符串值，序列化独立于类名/消息)。
- `RecoveryAction`(枚举)：ABORT / RETRY / COMPRESS / ROTATE_CREDENTIAL / FALLBACK / SHRINK_IMAGE / DOWNGRADE_TOOL_CONTENT / STRIP_REQUEST_STATE。

### 6.3 `handlers.py`
- `is_retryable(exc)`：控制流异常→False、MetaGPTError 看 `.retryable`、stdlib/SDK 瞬时类→True。
- `classify_llm_error(exc)`：原始 provider 错误 → 类型化 LLMError；`_classify_api_status_error` 按 HTTP 状态码 + 文本模式（billing/content_policy/context_window/image/multimodal 等模式组）消歧 400/403/404/413/429。循环处理 OpenAI + Anthropic 两套 SDK。

### 6.4 `recovery.py`
- `RecoveryRunner(strategies, max_recoveries=3)`：`run(call)` 重试循环——异常 → `_action_for` 解析 RecoveryAction → ABORT/超预算/无策略/策略返回 False 则重抛，True 则重试。best-effort 发 `RecoveryEvent` 到总线。

---

## 7. `common/events/` —— 事件总线

统一的有序异步事件脊柱，收敛了原先分散的"流式 sink / session recorder / hook fire-sites"三套机制。

### 7.1 模型
- **control 事件**（`is_control=True`，折叠 `HookOutcome` 影响宿主）：`UserPromptSubmitEvent`、`PreToolUseEvent`(可拒绝/改参)、`PostToolUseEvent`(可注入上下文)、`PreCompactEvent`(可否决)、`PostCompactEvent`、`SessionStartEvent`、`FileChangedEvent`。
- **observation 事件**（fire-and-forget）：`SessionEnd`、`TurnStart/End`、`MessageAppended`、`LLMStreamDelta/End`、`CompactionCheckpoint`、`FileSnapshot`、`FileMutated`、`Diagnostics`、`Recovery`、`TaskProgress`、`ResourceReport`。
- 每个事件 ClassVar `name`(判别串) + `is_control`。

### 7.2 `bus.py` `EventBus`
- `subscribe`(按 `priority` 升序插入)、`async emit(event)`(按序分发→折叠 outcome→失败 best-effort 记录)、`emit_sync(event)`(仅给有 `handle_sync` 的订阅者，供同步调用点的 observation 事件)。

### 7.3 `context.py`
- ContextVar `_ACTIVE_BUS`：`set_bus`(上下文管理器)/`current_bus`/`emit_event`/`emit_event_sync`——让深层调用点无需穿线即可发事件，无 bus 绑定时返回 EMPTY/no-op。

### 7.4 其余
- `outcome.py`：复用 hook 层（`EventOutcome = HookOutcome`，再导出 `fold`/`EMPTY`）。
- `log_subscriber.py` `LogSubscriber`(priority=90，最后)：每事件一行语义日志，生命周期事件 INFO、其余 DEBUG，`_clip` 截断长文本。

---

## 8. `common/hook/` —— 钩子系统

双路径设计：**Python 回调**（程序注册、零序列化）+ **外部命令**（配置驱动，JSON stdin/stdout，兼容 CC/codex）。

### 8.1 `types.py`
- `HookEvent` Literal：PreToolUse / PostToolUse / UserPromptSubmit / SessionStart / Stop / PreCompact / PostCompact / FileChanged。
- `HookInput`(含 `to_json_dict()` CC camelCase wire 格式)、`HookOutcome`(behavior/updated_args/additional_context/system_message/stop + `is_blocking`)。
- `fold(...)`：折叠多 handler 产出——behavior 优先级 **deny>ask>allow**，context 累加，stop 粘滞，updated_args 取最后。`EMPTY` 快路径。

### 8.2 `manager.py` `HookManager`
- `_MATCH_FIELD`：每事件的匹配字段（PreToolUse→tool_name、FileChanged→path、SessionStart→source…）。
- `register(event,fn,matcher)`、`_matches`(None/`*`=全部、`A|B`=精确列表、否则正则，畸形正则降级精确比对)。
- `async fire(event,payload,*,permission_mode)`：无匹配 handler → EMPTY；并发跑所有匹配 handler（回调在进程内、命令 spawn）→ 折叠。**永不抛**（每 handler 包裹，失败记录跳过）。

### 8.3 其余
- `command_handler.py`：`run_command_handler`——serialize HookInput 为 JSON 行喂 stdin、注入 `AGENT_PROJECT_DIR`/`AGENT_SESSION_ID`、超时 kill、解析 stdout。
- `parser.py`：`parse_command_output`(exit 2=CC 阻塞信号→deny；JSON→`_outcome_from_obj` 映射 decision/permissionDecision/updatedInput/additionalContext/continue 等 CC 契约字段)、`parse_callback_result`。
- `subscriber.py` `HookSubscriber`(priority=10，先于 recorder，否决在持久化前落地)：把 EventBus 事件映射到 `(hook_event_name, payload)` 再 `fire`。

---

## 9. `common/logs/` —— 日志系统

loguru + trace-id 上下文 + 装饰器/mixin 自动装配 + 流式事件发射。

- `core.py`：实例化 loguru（文件轮转 50MB/14 天/enqueue），格式含 trace_id 前缀。`define_log_level`、`suspend/resume_console_log`(REPL 用)。
- `context.py`（零依赖）：ContextVar `_TRACE_ID`，`bind_trace(trace_id=None)`(omit 时生成 12 字符 hex) / `current_trace_id()`。core patcher 自动给每条记录盖 trace_id。
- `decorator.py` `log_call`：函数级日志（entry args / exit result+elapsed / exception traceback），`opt(depth=2)` 指向调用点，`_LOGGED_MARKER` 防重复装配。
- `mixin.py` `LoggedMixin` / `@log_class` / `@no_log`：类级自动装配——`__init_subclass__` 包裹类自有公开方法。跳过私有/dunder/描述符/生成器/已装配。旋钮 `__log_level__`/`__log_exclude__` 等。与 pydantic/Singleton 元类兼容。
- `stream.py` `log_llm_stream`：同步函数（provider chunk 循环内调），发 `LLMStreamDeltaEvent` 到活动总线（无 bus 时 no-op）。
- `tool_output.py`：`ToolLogItem` + 可插拔 sink（`set_tool_output_logfunc[_async]`），路由工具输出到 reporter/web/disk。
- `human_input.py` `get_human_input`：可插拔输入源（默认 `input()`），sync/async 自适应，失败回退"Consent is presumed"。

> 风格约定：在关键类上用 `@log_class(level="DEBUG", exclude={...})` 自动装配（Role / ReActLoop / ToolExecutor / ThinkEngine / ContextManager 等），**不在方法体内手写** `logger.*`。

---

## 10. `common/prompt/` —— 提示词模板层

所有 model-facing / system 文本的单一来源，逻辑与文本解耦，便于审查。

- `role.py`：静态可缓存前缀（`PREFIX_TEMPLATE`/`SYSTEM_PROMPT` 行为硬契约）+ 动态段占位符（`${role_info}`/`${available_commands}`/`${command_guide}`/`${env_section}`/`${memory}`/`${output_format}` 等，位于 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 缓存边界下方）。`CMD_PROMPT`(每轮 user prompt，带 `${command_hint}`)。
- `output.py`（协议门控）：`OUTPUT_SECTION`(XML 命令块格式)、`XML_COMMAND_GUIDE`(教 `<end></end>`) vs `NATIVE_COMMAND_GUIDE`(无 end，结构化 tool call)、`XML_COMMAND_HINT` vs native 空串、`SUMMARIZE_STATUS_WHEN_CONSECUTIVE`。
- `tools.py`：各工具 model-facing 描述（Edit/Write/Read/NotebookEdit/Glob/Grep/Bash/Python/Terminal/Agent/AskHuman/ReplyToHuman + `APPLY_PATCH_GRAMMAR`）。
- `agent.py`：子 agent 委派（`AGENT_TASK_PROMPT`/`SUBAGENT_SECTION_TEMPLATE`/`SUBAGENT_EXAMPLE`）。
- `memory.py`：持久文件记忆（`MEMORY_INSTRUCTIONS` 静态 + `MEMORY_CONTEXT` 动态 + frontmatter 模板），镜像 CC auto-memory。
- `compaction.py`：autocompact 摘要提示（`NO_TOOLS_PREAMBLE/TRAILER` + 结构化分析 body），CC port。

---

## 11. `common/observability/` —— Langfuse 追踪

代码无侵入、懒导入、默认关。
- `init_langfuse(cfg)`：Config validator 一次性调用，设环境变量。`is_enabled()`/`steps_enabled()`。
- `make_async_openai(**kwargs)`：drop-in 工厂——启用时返回 langfuse 插桩客户端，否则原生 `openai.AsyncOpenAI`。
- `maybe_trace(session_id,...)` / `maybe_span(name,...)`：root/child span 上下文管理器，禁用时 nullcontext。所有 langfuse import 都在函数内懒加载。

---

## 12. `common/scheduling/` —— 周期循环原语

- `loop.py` `PeriodicLoop(interval, tick, name, sleep_first)`：tick 返回 `False` 停止，其余继续；best-effort（异常吞、CancelledError 传播）。`start()`/`cancel()`/`async stop()`/`is_running()`。各轮询子系统（ConfigWatcher / FileWatcher / StallDetector）注入自己的 tick 业务，循环统一管理生命周期。

---

## 13. `common/utils/` —— 工具函数层

| 类别 | 文件 | 用途 |
|------|------|------|
| LLM/Token | `token_counter.py` | `TOKEN_COSTS` 价格表 + tiktoken 计数（包级导出） |
| 解析/修复 | `stream_xml.py` / `repair_llm_raw_output.py` / `custom_decoder.py` | 流式 XML 命令提取、LLM 输出修复、宽松 JSON 解码 |
| Markdown | `markdown_meta_parser.py` / `action_node.py` | frontmatter+section 解析、结构化数据 |
| 异步 | `async_helper.py` | `run_coroutine_sync`（检测运行中 loop，防死锁） |
| 磁盘 IO | `disk_io.py` | 无状态文件读写原语（tool 结果持久化复用） |
| 配置 | `yaml_model.py` / `pydantic_compat.py` | YamlModel 基类、pydantic v2 兼容垫片 |
| 报告 | `report.py` | 流式 token bus 订阅者、`CURRENT_ROLE` contextvar |
| Git 状态 | `git_state/{collector,render}.py` | 只读 repo 状态（filesystem-first 读 `.git/HEAD` + shell out status/log，1.5s TTL 缓存）+ 渲染。`find_git_root` 处理 `.git` 文件指针 |
| 错误/调试 | `exceptions.py` / `sentry.py` / `prompt_sanitizer.py` / `parse_docstring.py` | 异常处理、Sentry、文本清洗、docstring 解析 |
| 媒体 | `role_zero_utils.py` | 图像/PDF 提取编码 |
| 杂项 | `json_to_markdown.py` / `serialize.py` / `remote.py` | 格式转换、schema flatten、远程任务桩 |

`__init__.py` 仅导出 `TOKEN_COSTS`/`count_message_tokens`/`count_string_tokens`，避免与 llm_config 的导入环。

---

## 分层依赖总结

```
        ┌─────────────────────────────────────────────┐
        │  上层：roles / executor / context / router /  │
        │        session / environment / tasks         │
        └───────────────┬──────────────▲──────────────┘
                        │ import        │ 注入具体实现
                        ▼               │ (满足 Protocol)
        ┌─────────────────────────────────────────────┐
        │             common (本层)                     │
        │  base(ABC) · interface(Protocol) · schema ·  │
        │  const · config · exception · events · hook ·│
        │  logs · prompt · observability · scheduling ·│
        │  utils                                        │
        └───────────────┬──────────────────────────────┘
                        │ import
                        ▼
        ┌─────────────────────────────────────────────┐
        │     标准库 + 第三方 (pydantic/loguru/...)      │
        └─────────────────────────────────────────────┘
```

- **单向依赖**：`common` 只向下依赖标准库/第三方，绝不 import 上层。
- **依赖倒置**：上层经 `common/interface/` Protocol 注入具体实现，彼此解耦。
- **泄漏防护**：`interface/`(LEAF)、`schema/permission_types.py`、`schema/node_status.py`、`logs/context.py` 等均为零业务依赖，可在任意层安全 import。

---

## `metagpt.roles` —— Role 抽象核心

`roles` 是框架的**中枢编排层**，定义 `Role`——agent 的运行实体。核心设计哲学：**组合优于继承 + 懒初始化 + 窄能力面 + 事件驱动**。

> Role 不是 Pydantic 模型，而是纯编排器：把静态配置（RoleSchema）、可序列化运行态（RoleState）、惰性装配的子系统（RoleComponents）三者拼装起来，自身只保留薄薄的属性面与能力面。

### 三大支柱

| 支柱 | 类型 | 职责 |
|------|------|------|
| `role_schema: RoleSchema` | Pydantic（静态部署期配置） | tools / 协议 / 权限 / LSP / file-watch / 提示词模板 |
| `state: RoleState` | Pydantic（可序列化运行态） | session_id / cwd / 消息上下文 / 恢复标志 |
| `_components: RoleComponents` | 惰性装配持有者 | router / executor / think_engine / loop / 总线 等全部子系统 |

### 1. `role.py` —— Role 类

**职责**：编排器 + 消息发布者，把循环逻辑**委托给 ReActLoop**，把组件装配**委托给 RoleComponents**。

- **组件属性（懒委托）**：访问即惰性构造，全部转发到 RoleComponents 的 slot——`router`、`skill_manager`、`bg_pool`、`executor`、`context_manager`、`session_log`、`event_bus`、`file_snapshot_recorder`、`hook_manager`(opt-in→None)、`lsp_service`(opt-in)、`diagnostics_buffer`(opt-in)、`file_watch_service`(opt-in)、`turn_context_bus`(总在)、`think_engine`、`command_channel`、`context_provider`。
- **`run(with_message)` 流程**：
  1. `bind_trace(session_id)` 关联本次运行所有日志 + `set_bus(event_bus)` 让深层调用点（LLM 流式、快照）能发事件。
  2. `_ensure_ready()`：物化 ContextManager、skill_manager、executor MCP。
  3. 一次性发 `SessionStartEvent`（记录身份），按配置启动 file-watch service。
  4. 摄入 user prompt：发 `UserPromptSubmitEvent`，hook outcome 可注入上下文或中止；消息入 msg_buffer。
  5. `loop = _make_loop()` → `await loop.run()` 跑 think/act/compaction 循环直到去激活或达上限。
  6. турn 边界：`_emit_turn_end()` 发 `TurnEndEvent`（recorder 标记 turn、hook 触发 Stop）。
  7. 收尾：去激活、给响应打 display_name 标签、发布到 env 或本地 buffer。
- **能力面 `tool_capabilities()`**：显式 allowlist 的薄包装器（`get_cwd`/`set_cwd`/`record_file_read`/`record_file_snapshot`/`ask_human`/`request_approval`/`reply_to_human`/`end_session`/`wait_interruptible`/`deactivate`）。工具**只能**调这些，绝不直接触碰 RoleState/memory/其他属性。
- **序列化**：`dump()` 只存 `state` + `role_schema`（不存组件，load 时重建）。注册到 `BaseRole._ROLE_REGISTRY`。
- **会话恢复**：`resume_session()`——经 `session.replay()` 重建历史，**直接赋值** `state.context.messages[:]`（绕过 ContextManager 不重复记录），恢复 cwd/project_root，置 `recovered=True`。`fork_session()`——播种新 session（带 parent_session_id 血缘）构造同类兄弟 Role。
- **`cleanup()`**：停 file-watch、关 LSP servers、`ToolExecutor.cleanup()`（关 terminal/kernel），best-effort 幂等。
- **`_resolve_shell_tools`**：去重保序，**Bash 与 Terminal 并存**（Bash 一次性 jam-proof / Terminal 持久 PTY）。

### 2. `role_schema.py` —— RoleSchema（静态配置）

部署期声明、运行时不变。关键字段：身份（name/profile/goal/constraints）、提示词模板（system_prompt/cmd_prompt/instruction/summary_prompt）、`command_protocol: Literal["xml","native"]`（默认 native）、循环控制（max_react_loop=50/max_consecutive_react_limit=10）、`tools`/`mcps`/`agents`/`skills`、`permissions`(opt-in PermissionConfig)、`hooks`(opt-in)、`lsp`(opt-in)、`file_watch`(opt-in)、`record_file_history`(True)/`snapshot_backend`("auto")、记忆/摘要（enable_memory/memory_k=30/use_summary）。tool-spec 信封（openai vs anthropic）**不在此配**，运行时按 LLM config 推断以匹配 wire 协议。

### 3. `role_state.py` —— RoleState + RoleStateController

- **`RoleState`**（Pydantic + SerializationMixin）：可序列化运行快照。`context:LLMCallContext`（喂 LLM 的消息序列）、`msg_buffer:MessageQueue`(exclude)、`session_id`、`parent_session_id`、`working_dir`/`original_working_dir`/`project_root`、`latest_observed_msg`、`recovered`、`addresses`/`watch`、PrivateAttr `_active`(循环开关)/`_file_read_state`(path→mtime_ns)。
- **`RoleStateController`**：DTO 之上的行为层（保持 state 纯净）。`get_cwd`(fallback 永不空)/`set_cwd`/`record_file_read`/`get_file_read_mtime`/`is_active`/`deactivate`/`put_message`/`is_idle`。Role 暴露薄委托到这些方法，工具不触碰原始 state。

### 4. `role_components.py` —— 组件装配与接线

惰性 slot + builder：访问即构造，`peek_*` 返回原始 slot 不触发构造（供 teardown/turn 边界）。持有对 Role 的反向引用以读取 schema/state/config。
- **LLMRouter**：每 Role 一个，绑定 `role.context`（注册表+实例缓存一致）。
- **ContextManager + SessionLog**：ContextManager 背靠 `RoleState.context`（跨 checkpoint 存活），用 compress LLM 做 autocompact；SessionLog 首次访问时建并写 session_meta，被 RecorderSubscriber 与 FileSnapshotRecorder 共享。
- **EventBus + 订阅者**（优先级序：hook→recorder→logger）：`HookSubscriber`(opt-in)、`RecorderSubscriber`(总接)、`LogSubscriber`、`CompactionNoticeContextSource`、`ReporterSubscriber`(env URL 时)、`LspService`(opt-in 输入订 FileMutatedEvent/输出播 DiagnosticsEvent)、`DiagnosticsBuffer`(opt-in)。
- **opt-in 子系统零开销**：hook/lsp/file_watch/permission 未配置时返回 None，所有 fire-site 短路。
- **TurnContextBus（总在）**：聚合 4-5 个 per-turn ephemeral 源（Git/TokenPressure/CompactionNotice/BackgroundTask/DiagnosticsBuffer），按 priority 渲染合并进 user prompt 的 `<system-reminder>`（不缓存、不进 history）。

### 5. `context_provider/` —— 请求装配管线

- **`ThinkRequest`**（dataclass）：ContextProvider 的纯数据产物，打包 `req`(system+history+user)/`system_prompt`/`state_data`/`tool_specs`。
- **`BaseContextProvider`(ABC)**：窄接口——`prepare()->ThinkRequest`/`loop_context()->LoopContext`/`resolve_llm()->LLMClient`。**不暴露 Role**，使 loop 保持 role-agnostic。
- **`ContextProvider`**（具体）：**只读** Role，做脏活——渲染 prompt、格式化请求、解析 tool specs。`prepare()` 经 PromptBuilder + `context_manager.prepare_request`(跑 micro/autocompact) 装配；`_think_inputs()` 解包 RoleSchema 身份；`_think_subsystems()` 交付 live 协作者（含 turn_context_bus）。

### 6. `lsp/` 子包 —— 语言服务诊断（opt-in MVP，service-only）

输入边订 `FileMutatedEvent`、输出边播 `DiagnosticsEvent`；per-Role session（非全局单例）；best-effort（坏 server 不破坏 turn）。
- `jsonrpc.py` `JsonRpcEndpoint`：裸 Content-Length JSON-RPC 2.0 传输（不懂 LSP 语义），request(future 关联+timeout)/notify/close，后台 `_read_loop` 分发。
- `registry.py` `DiagnosticRegistry`：per-file last-write-wins + 变更检测，`drain_changed`（per-file cap 10/total 30，errors 优先，清空文件报 resolved 一次）。
- `format.py` `format_diagnostics`：`<lsp_diagnostics>` block，0-based→1-based。
- `server.py` `LspServerInstance`：一 server 子进程 + LSP 语义（start 握手/did_save full-sync/shutdown），`alive` 失败后 no-op。
- `manager.py` `LspServerManager`：per-session 持有 servers + 共享 registry，`server_for` 懒启动（lock 双检，`_failed` 不重试）。
- `service.py` `LspService`：双角色 bus 订阅者+生产者，`file_saved`/`drain_diagnostics`/`shutdown`，`bus=None` 时禁 emit。
- `buffer.py` `DiagnosticsBuffer`：push→pull 桥（既是 EventSubscriber 又是 EphemeralContextSource），edits 事件驱动入、think cycle 拉一次出，drain 后清空。

---

## `metagpt.session` —— 会话持久化层

崩溃安全的**追加式 JSONL 事件日志**系统，作为 agent 会话的耐久真相源（吸取 codex rollout + claude transcript）。

> 分层：`session` **只依赖 `common.*`**，零跨层 import（无 roles/context）；由上层经 Protocol 注入消费。文件布局 `{workspace}/.agent_sessions/{session_id}/rollout.jsonl`（+ 可选 `blobs/`、`git/`）。

### 核心设计要点
- **追加式 JSONL**：崩溃安全、单一真相源、无损坏风险。
- **正向单次重放**：compaction 检查点自包含完整历史，无需 codex 式反扫。
- **best-effort 记录**：会话失败永不破坏 turn（记录、吞掉）。
- **双 blob 底座**：代码工作区自动切 git，否则原始 sha256 blob。
- **事件总线订阅者**：屏幕（renderer）与磁盘（recorder）由同一事件流喂养，不会发散。
- **无二级索引**：rollout 为真相源，listing 用 head/tail 窗口。

### 1. `events.py` —— tagged-union 事件 schema
`SCHEMA_VERSION=1`。行格式 `{type, ts, payload}`。6 类事件：
- `SessionMetaEvent`(首行，身份+血缘 parent_session_id+cwd/project/model)、`MessageEvent`(`message.dump()` 无损往返)、`CompactedEvent`(`replacement_history`=压缩后完整扁平历史，支撑单次重放)、`TurnContextEvent`(turn 快照，重放忽略)、`MetaUpdateEvent`(title/last_prompt 追加到尾，listing 用)、`FileSnapshotEvent`(before-image，带 `backend` 标签供读侧路由)。
- `to_line`/`parse_line`（容错：空/坏行→None，不中断扫描）。

### 2. `log.py` —— `SessionLog`
物理日志管理器。`create(meta)`(写首行，已存在则 no-op 幂等)、`append(event)`(O_APPEND+flush 崩溃安全)、`iter_raw()`(容错读，跳坏行)、`path`/`exists()`。

### 3. `replay.py` —— 历史重建
`replay(log)->ReplayResult`(messages/meta/checkpoints/skipped)。单次正向扫描：`message`→append、`compacted`→**RESET** 为 replacement_history、`session_meta`→存 meta、其余忽略。`Message.load` 失败跳过计入 skipped。最终历史 = 最后检查点 + 其后 message。

### 4. `listing.py` —— 会话发现（lite 策略）
不全量 parse。`SessionInfo` 由两窗口拼成：HEAD(前 16 行取 session_meta + 首条 message preview) + TAIL(末 64KB 反扫最新 meta_update 取 title/last_prompt)。`list_sessions(base_dir,*,cwd)` 按 mtime 倒序，cwd 过滤（==working_dir 或 project_root）。

### 5. `fork.py` —— 会话血缘
`fork(source,*,new_session_id,base_dir)->str`（纯磁盘，不需 Role）：replay 父→为子建 session_meta(parent_session_id=source+复制锚点)→继承历史逐条 append。源缺→FileNotFoundError，目标已存在→FileExistsError。子从父最终状态起步，完全独立。

### 6. `snapshot.py` —— 文件历史快照（内容寻址）
- **`BlobStore`**(默认)：sha256 内容寻址 blob，`{session_dir}/blobs/{hash[:2]}/{hash}`，原子写+去重。
- **`GitBlobStore`**(代码工作区)：独立 bare 仓 `{session_dir}/git`(非用户仓，隔离避免被 gc)，shell out `git hash-object`/`cat-file`，sha1。
- `detect_blob_backend(working_dir)`：有 git 二进制 + 在 repo→"git"，否则"blob"（复用 `common.git_state.find_git_root`）。`make_blob_store` 工厂。
- **`FileSnapshotRecorder`**(实现 `FileSnapshotStore` Protocol)：持共享 SessionLog + blob store，`snapshot(full_path,*,tool)` 写盘前记 before-image（不存在→create/可读→update+put blob，best-effort 永不抛），事件盖 `backend`。executor 文件改写工具写盘前调用。

### 7. `history.py` —— 文件历史读侧
`SnapshotEntry`(path/operation/pre_hash/backend/index)。`file_history(log)`(按 path 分组时序)、`diff_snapshot(log,path,*,index=-1)`(before-image vs 现盘 unified diff)、`restore(...)`(写 blob 回盘，create 则删文件)。均 backend-aware（`_blobs_for` 按 entry.backend 选库）。

### 8. `subscribers.py` —— 事件总线集成
**会话记录现在是 bus 订阅者**（不再是注入 ContextManager 的 sink）。`RecorderSubscriber`(priority=80，hook 否决之后)：`MessageAppendedEvent`→MessageEvent、`CompactionCheckpointEvent`→CompactedEvent、`TurnEndEvent`→TurnContextEvent、`SessionStartEvent`→`SessionLog.create`。`enabled` 开关(resume 重放时关)，best-effort 永不破坏 bus。同一事件流喂屏幕+磁盘，防发散。

---

## `metagpt.environment` —— 多 agent 运行时

多 agent 控制平面（移植自 codex `agent/*`），事件驱动调度器 + 层级 agent path + per-agent 邮箱 + LRU 驻留淘汰 + turn 原子消息投递。

### 1. `control.py` —— `AgentControl`（中枢）
session 作用域的编排中心，liveness 单一真相源。聚合 `AgentRegistry`/`AgentExecutionLimiter`/`Residency`/`EventDrivenScheduler`/`ResidencyStore`，持 live runtime 映射 `{session_id→AgentRuntime}`。
- 投递：`send_input(agent_id,message,mode)`(入队+可选唤醒)、`send_inter_agent_communication(...)`。
- `_ensure_loaded`（被淘汰 agent 按需 rehydrate）、`start_completion_watcher`（子达终态通知父）。
- 生命周期 `start/stop/run(k)`、`add_agent`、`resolve_agent_reference`、`get_status`。

### 2. `runtime.py` —— `AgentRuntime`
包一个 live `Role`(鸭子类型，不 import Role) + 调度状态。持 `role`/`mailbox`/`agent_path`/`status`/`wake_event`/`active_turn`。状态机 `AgentStatus`(IDLE→RUNNING→{COMPLETED/ERRORED/INTERRUPTED}, NOT_FOUND)。`run_one_turn`(锁内跑一次 role.run)、`is_unloadable`(终态∧无 active turn∧空 mailbox∧空 msg_buffer 才可淘汰)、`wake`/`shutdown`。

### 3. `registry.py` —— `AgentRegistry`
session 作用域簿记：agent 树拓扑、nickname 池、总数（强制 max-threads 上限）。`SpawnReservation`(RAII 原子 spawn/回滚)、`reserve_spawn_slot`(超限→AgentLimitReached)、`register_root_thread`、各种 lookup。`AgentMetadata`(agent_id/path/nickname/role)。线程安全（Lock）。

### 4. `mailbox.py` —— 邮箱与投递
per-agent 入站队列，**调度器**（非 ReActLoop）在 turn 边界 drain → turn 原子投递（中途注入的邮件自然延迟）。`DeliveryMode`(TRIGGER_TURN 入队+唤醒 / QUEUE_ONLY 仅入队)、`InterAgentCommunication`(author/recipient/content→UserMessage)、`Mailbox`(`enqueue`/`drain_for_turn`/`dump`/`load`，`_data_event` 独立于 `wake_event`)。

### 5. `residency.py` + `store.py` —— 驻留与淘汰
idle agent LRU 淘汰落盘 + 按需 rehydrate。
- **`Residency`**：LRU deque，`reserve_slot(capacity,protected)`(淘汰 LRU 直到腾出 slot)、`touch`、`ResidencySlot`(RAII)。线程安全（同步状态加锁，异步 I/O 锁外）。
- **`ResidencyStore`**：`ResidencyRecord{role_dump,mailbox_dump,msg_buffer_dump}`——**历史不存这里**（rollout.jsonl 为真相源）。`materialize` 在 rollout 存在时 `_strip_history`(清 `state.context.messages` 避免双写)；`rehydrate` 经 `session.replay()` 把历史灌回。与 rollout 互补：rollout 存历史，residency 存配置+运行态(msg_buffer/mailbox)。

### 6. `agent_path.py` —— `AgentPath`
层级路由键（独立于 session_id）。绝对路径 `/root/researcher/worker`，校验（须 `/root` 开头、小写+数字+下划线）。操作 `root`/`join`/`parent`/`resolve`/`name`。Pydantic 集成（str↔AgentPath 自动转换）。

### 7. `limiter.py` —— 并发限制器
封顶并发**运行中** turn（正交于 registry 总 agent 上限）。`AgentExecutionLimiter`(`initialize`/`ensure_capacity`/`guard()`→`AgentExecutionGuard` RAII 增减 `_active`)。

### 8. `scheduler.py` —— 事件驱动调度器
`EventDrivenScheduler`：每 runtime 一个 asyncio driver task，泊在 `wake_event` 上，每唤醒跑一 turn。持久模式(`start/stop` driver 永久循环) vs barrier pump(`run(k)` 有界推进，供测试)。`_driver`(park→clear→`_stage_mailbox`(turn 边界 drain mailbox 推入 msg_buffer，ReActLoop 永不见中途邮件)→`_run_turn_safe`)。`quiescent`(无运行+无待触发)。可选 limiter guard。

### 9. `base_env.py` + `mgx/mgx_env.py`
`AgentEnvironment(BaseEnvironment)`：旧 `BaseEnvironment` API 包控制平面。`add_role`(包 AgentRuntime 注册)、`publish_message`(解析 send_to→`control.send_input`，广播只到已加载 agent、定向地址透明 rehydrate)、`run(k)`/`quiescent`/`stop`。human channel 桩（MGXEnv override `ask_human`/`reply_to_human`）。

### 10. `scheduling/` 子包 —— cron 定时任务
确定性 cron + jitter 驱动的 prompt 注入。耐久(磁盘) + session-only 任务，单写锁防双触发。
- `task.py` `CronTask`(id/cron/prompt/recurring/permanent/durable/target_session_id) + `CronJitterConfig`。
- `cron.py`：`parse_cron_expression`(5 字段，支持 `*`//`a-b`/列表)、`compute_next_cron_run`(逐分钟前推，DOM/DOW vixie OR)、jitter 函数(确定性 task_id→[0,1))、`cron_to_human`。
- `scheduler.py` `CronScheduler`(控制平面无关，注入 `on_fire`)：每 1s tick 触发到期任务。磁盘为真相(next-fire 从 last_fired_at 算，存活重启)、单写锁、热重载(mtime 轮询)、idle 门控、缺失补偿、jitter+到期。clock 可注入测试。
- `service.py` `CronService`(注入 on_fire→`control.send_input`)、`store.py` `CronTaskStore`(50 任务上限)、`lock.py` `SchedulerLock`(文件系统单写锁)。

### 11. `watching/` 子包 —— 文件监视
依赖无关 mtime+size 轮询（同 ConfigWatcher）。
- `events.py` `FileChangeEvent`(frozen: path/change_type/mtime/size)。
- `watcher.py` `FileWatcher`(控制平面无关)：轮询 roots(os.walk 递归+单文件)，`ignore` fnmatch，`_state` 基线。`note_self_write(path)` 抑制 agent 自写回声。纯同步 `_snapshot`/`_diff` 可独立测。
- `service.py` `FileWatchService`(glue)：`_on_change`→fire `"FileChanged"` hook(best-effort)；订 `FileMutatedEvent`→`note_self_write`。

### 12. `exceptions.py`
从 `common.exception` 再导出：`AgentControlError`/`AgentLimitReached`/`AgentNotFound`/`AgentNotKnown`/`AgentPathExists`。

### 关键设计模式
- **RAII 上下文管理器**：`SpawnReservation`/`ResidencySlot`/`AgentExecutionGuard` 退出自动回滚/释放。
- **Protocol/回调注入**：Residency 用 `RuntimeLookup/Remover` 回调（不持 live 映射）、CronScheduler 用 `on_fire`、FileWatcher 用 `on_change`——均控制平面无关。
- **turn 原子邮箱**：调度器在边界 drain（非中途），延迟注入天然免费。
- **LRU 驻留 + 磁盘真相**：rollout 存历史、residency 存配置+运行态，投递时透明 rehydrate。
- **鸭子类型解耦**：AgentRuntime 不 import Role；环境层经 rollout 复用会话历史。

---

## metagpt.executor —— 工具执行引擎（act 侧）

executor 是与 think 侧（ThinkEngine）对偶的**行动侧编排引擎**：把 LLM 产生的命令派发为真实工具调用，并在单一咽喉点串起参数校验、权限审批、事件钩子、错误恢复、结果限流、文件快照与 LSP 回流。所有工具都是一等的 `BaseTool` 实例，经自包含注册表自动发现；MCP 动态工具被适配成同一基类，共用同一条派发路径。

### 目录与分层
```
executor/
├── base_executor.py     # BaseToolExecutor ABC（react loop / channel / prompt builder 消费的契约）
├── base_tool.py         # BaseTool 通用基类（identity / requires / schema / 权限钩子）
├── tool_executor.py     # ToolExecutor 具体实现（单派发咽喉 run_command）
├── tool_registry.py     # @register_tool 自包含发现（扫 metagpt.executor.tools）
├── agent_registry.py    # @register_agent 可孵化 agent 发现（扫 metagpt.roles.agents）
├── tool_result.py       # ToolResult dataclass（output/success/data/images/pdfs）
├── tool_result_limit.py # 单工具结果限流 + 大结果落盘 <persisted-output>（CC 移植）
├── tool_retry.py        # @retryable_tool 瞬时失败重试（tenacity）
├── tool_convert.py      # 函数/类 → schema（docstring + AST）
├── tool_spec_adapter.py # MetaGPT schema → native tool-use spec（JSON Schema，含 pydantic 展开）
├── mcp_adapter.py       # MCPToolAdapter（把发现的 MCP 工具包成 BaseTool）
├── tools/               # 具体工具实现（read/write/edit/bash/terminal/python/glob/grep/...）
├── dependency/          # 工具底层引擎（_terminal PTY / _kernel Jupyter / _file_base / _apply_patch / _document）
├── permission/          # 双轴权限子系统（审批轴 + 沙箱轴）
├── tasks/               # 后台任务系统（pool / attachment / bggraph DAG / 落盘 / stall）
└── mcp/                 # MCP 集成（stdio/sse client / registry / universal / manager）
```

### 1. BaseToolExecutor（`base_executor.py`）
工具执行的 ABC 契约，允许替换实现（远程 / 沙箱）：
- `async run_command(name, kwargs=None, *, result_id=None) -> ToolResult`：按名派发单次工具调用。
- `get_native_tool_specs(provider="anthropic") -> list[dict]`：native channel 的全量工具 spec。
- `get_tool_schemas()`/`get_mcp_tool_schemas()`：内置工具 / MCP 工具 schema（主名 → schema）。

### 2. BaseTool（`base_tool.py`）
万能工具基类，类变量定义身份与契约：
- **身份**：`name`（主名，查找键）、`aliases`（别名，LLM 可任选）、`description`（空则取 `call()` docstring 首行）。
- **能力注入** `requires: tuple[str,...]`：声明所需 Role 能力（方法）名。`bind(session_id, role)` 仅注入这些命名属性，且必须落在 `Role.tool_capabilities()` 显式白名单内——工具**永远拿不到** RoleState、memory、Role 本体（能力注入而非反射）。
- **限流/风险**：`max_result_size_chars`（单工具结果上限）、`risk_level`（"low/medium/high" 咨询性）、`mutates_filesystem`（驱动 acceptEdits 模式 + FileMutatedEvent）。
- **核心方法**：`async call(**kwargs)`（抽象入口，参数全是 LLM 指定，框架上下文只经 `self.session_id`）、`bind()`、`cleanup_session()`。
- **权限钩子**（被 PermissionEngine 在 call 前消费）：`permission_target(args)->str`（单目标，如 Bash 返回 command、文件工具返回 path）、`permission_targets(args)->list[str]`（多目标，如 ApplyPatch 列出每条 path）、`check_permissions(args)->PermissionDecision|None`（工具自检，返回 deny/ask **免 bypass**，返回 None 则下沉到规则/模式）。
- **Schema 生成**：`get_schema()`（XML 路径，`{name,description,parameters}`，由签名+docstring 自动生成；`custom_schema()` 为逃生口）、`get_native_schema()`（native 路径，`{name,description,input_schema}`，input_schema 为 JSON Schema）。**注意**：旧 XML 协议把每个参数都当字符串解析（无类型信息），结构化（非标量）参数工具仅在 native channel 正确工作。

### 3. ToolExecutor（`tool_executor.py`）—— 单派发咽喉
每 Role session 一个实例（实例隔离，避免并发 bind 冲突）。构造时预 bind 静态工具，MCP 动态工具经 `register_tool_instance()` 注入同一 `_tools` map。`run_command` 是**所有工具调用的唯一咽喉**，端到端流程：
1. **工具解析**：按名/别名查 `_tools`，缺失 → `ToolResult(success=False)`。
2. **参数预校验**：LLM 参数对 `call()` 签名校验，缺必填/多余 → `ToolValidationError`（XML 路径跳过类型强校验）。
3. **PreToolUse 事件**（有 bus）：订阅者（hook 层）可改写 args 或直接 block。
4. **权限闸**（opt-in）：`permission_targets` 多目标走 `check_multi()` 否则 `check()`；先跑工具 `check_permissions` 自检（免 bypass）；deny 即返回，或经 `updated_args` 收窄。
5. **恢复循环**：`_recovery_runner.run(_call)`，`RetryableToolError` 自动重试、`ToolError` abort、其他异常被捕获包成失败 result。
6. **结果归一**：`ToolResult.from_tool_return(raw)`——纯值**永远视为成功**（失败必须结构化 raise/return，故成功输出可以 "Error:" 开头）；`BgTaskResult` 透传进 `.data` 交 Role 处理后台提交。
7. **PostToolUse 事件**（有 bus）：可追加上下文或 block result。
8. **FileMutatedEvent**（success & mutates_filesystem & bus）：发 path 供 LSP 同步、watcher 抑制回声。
9. **结果限流** `_limit_result()`：按工具 `max_result_size_chars` 截断，大结果落盘换 `<persisted-output>` 预览（含媒体则原样直传）。
另有 `init_mcp()`/`cleanup()`（退出时逐唯一实例 `cleanup_session`）与 schema 自省（去重别名）。

### 4. 自包含注册表（`tool_registry.py` / `agent_registry.py`）
- **ToolRegistry**：单例，`discover()` 递归 import `_SCAN_PACKAGES=("metagpt.executor.tools",)` 触发每个 `@register_tool`（白名单扫描，快且无副作用；导入失败模块跳过）。`register(cls)` 校验未覆盖冻结方法（bind/session_id）、名字不与异类冲突（同类幂等）。便捷别名 `register_tool=registry.register`。
- **AgentRegistry**：镜像同模式，扫 `metagpt.roles.agents`，`@register_agent` 强制注册类是 Role 子类（孵化契约）。

### 5. 结果类型与限流（`tool_result.py` / `tool_result_limit.py`）
- `ToolResult`(dataclass)：`output:str`（给 LLM 的文本）/`success`/`data`（给 hook 的结构化原值）/`images`/`pdfs`（base64）。
- 限流（CC 移植）：`persistence_threshold(declared)` 把工具声明上限钳到系统默认（inf=硬退出）；`enforce_tool_result_limit(...)`——超阈写 session 作用域文件、内联换 `<persisted-output>` 预览（含全长+路径+首段 slice），`result_id` 稳定保证幂等（已 persisted 则 no-op）；persist=False 时 head 截断带丢弃注记。落盘走 `common.utils.disk_io`（与 tasks/disk_output 共用）。**分层**：限流住 executor（工具执行关切），依赖朝下 context→executor 永不反向。

### 6. Schema 适配（`tool_convert.py` / `tool_spec_adapter.py`）
- `tool_convert`：函数/类 → schema（`function_docstring_to_schema` 用 inspect.signature + Google docstring；`convert_code_to_tool_schema_ast` 用 `CodeVisitor` 走 AST）。
- `tool_spec_adapter`（纯函数零副作用）：`build_json_schema(call_fn)` 由签名+docstring 建 JSON Schema object——**pydantic BaseModel（或 list[Model]）参数自动展开为完整嵌套 schema**，富输入工具无需手写 schema；`to_native_tool_specs(schemas, provider)` 包 envelope（Anthropic `{name,description,input_schema}` / OpenAI `{type:function,function:{...}}`）。类型映射 `_json_type`/`_unwrap_optional` 处理 Optional/Union/泛型/pydantic，未识别兜底 string。

### 7. 重试（`tool_retry.py`）
`@retryable_tool(max_attempts=3, max_wait=10.0, retry_on=None)`：装饰工具 `call` 自动重试瞬时失败，predicate 默认 `is_retryable`（`RetryableToolError` 重试、纯 `ToolError` 不重试），tenacity 随机指数退避 + `reraise=True`（耗尽后原异常上抛，由 run_command 收成失败 result）。

### 8. 工具实现（`tools/`）
| 工具 | 类 | 改盘 | 风险 | requires | 要点 |
|------|-----|------|------|----------|------|
| Read | `Read` | 否 | low | record_file_read | 行号文本/图片/PDF/notebook/富文档；offset/limit；session 去重缓存 |
| Write | `Write(FileMutatingTool)` | 是 | high | get_file_read_mtime, record_file_read, record_file_snapshot | 读后写守卫 + CRLF/LF 保留 + 写前快照 |
| Edit | `Edit(FileMutatingTool)` | 是 | high | 同上 | 5 趟宽容匹配（exact→引号/制表归一）；空 old_string 建新文件 |
| NotebookEdit | `NotebookEdit(FileMutatingTool)` | 是 | high | 同上 | replace/insert/delete；cell id 或 "cell-N" 定位 |
| ApplyPatch | `ApplyPatch(FileMutatingTool)` | 是 | high | 同上 | 多文件 Add/Update/Delete/Move；事务式（先全验再写）；per-path 权限折叠 |
| Bash | `Bash` | 可能 | high | get_cwd, set_cwd | 一次性子进程，探针 pwd 同步 cwd；classify_command 预检 |
| Terminal | `Terminal` | 可能 | high | get_cwd | 持久 PTY；input/interrupt/close；前台程序驻留 |
| Python | `Python`（别名 Jupyter）| 可能 | high | get_cwd | 持久 Jupyter kernel；code/interrupt/restart/close；超时自动 interrupt |
| Glob | `Glob` | 否 | low | — | ripgrep --files，按 mtime 排序，capped 100 |
| Grep | `Grep` | 否 | low | — | 三模式 files/content/count；富文档抽取；与 Read 共用行号 |
| Ask / Reply | `AskHuman`/`ReplyToHuman` | 否 | low | ask_human / reply_to_human | 与用户文本通道 |
| AskUserQuestion | `AskUserQuestion` | 否 | low | ask_human | 1-4 题多选 + auto Other（native only）|
| End | `End` | 否 | low | end_session | 结束会话 |
| Sleep | `Sleep` | 否 | low | wait_interruptible | 可中断等待，新消息/后台完成唤醒 |
| Agent | `Agent` | 否 | low | — | 孵化 typed 子 agent，schema 动态嵌可用类型 |

### 9. 底层引擎（`dependency/`）
- `_file_base.py` `FileMutatingTool`（抽象基，未注册）：共享 `_check_read_before_write`（未读/mtime 变 → ToolError）、`_detect_line_ending`（嗅 64KB）、`_refresh_read_state`、`_snapshot_pre_write`（写前 before-image，best-effort）。
- `_terminal.py`：`TerminalSession`（PS1 哨兵法 `PROMPT_COMMAND` 打 `__TERM_<nonce>_$?__END` 标记，正则识 prompt+exit code；`HeadTailBuffer` 1MiB head/tail）+ `TerminalManager`（per-session 懒建）+ 模块单例 `TERMINAL`。
- `_kernel.py`：`KernelSession`（`jupyter_client.AsyncKernelManager`，drain iopub 到 idle，超时自动 interrupt+partial，ANSI strip）+ `KernelManager` + 单例 `KERNELS`；`cleanup_session` 同步 SIGKILL。
- `_document.py`：Grep/Read 共用统一行号（`document_lines=text.split("\n")` 非 splitlines）；`extract_pdf_text`（PyMuPDF/pdfminer/pypdf 依次）/docx/xlsx；无依赖静默跳过。
- `_apply_patch/`：`parser.py`（codex patch 格式 → AddFile/DeleteFile/UpdateFile/chunk）+ `seek.py`（5 趟模糊行序匹配 exact→rstrip→strip→Unicode 标点归一，EOF 偏向尾部）+ `applier.py`（纯计算无 IO，按 chunk 应用产新内容）。

### 10. 权限子系统（`permission/`）—— 双轴
**轴 A 审批**（要不要问用户）+ **轴 B 沙箱**（能不能碰这条 path），正交。核心模块只 import `common/schema`，零 executor 依赖避免环。
- **数据类型**（`common/schema/permission_types.py`）：`PermissionMode`（default/acceptEdits/plan/bypass/dontAsk）、`PermissionBehavior`（allow/deny/ask）、`PermissionDecision`（带 `allow/deny/ask` 类方法 + DecisionReason）、`PermissionRule`（tool_name + fnmatch pattern + behavior + source）。
- **classifier.py**（codex command_safety 移植，确定性零依赖）：`classify_command(cmd)->SafetyAssessment{known_safe,risk,reason}`——破坏性正则（`rm -rf`/fork bomb/`mkfs`/`dd of=/dev/`/`sudo`/`chmod 777`/`curl|sh`）→ 高危免 bypass ask；重定向 `>`/命令替换 `$()` 失格；按分隔符切段逐段验白名单（`_SAFE_COMMANDS`/`_SAFE_GIT_SUBCOMMANDS`，`git config` 仅 `--get/--list` 安全，`find` 有 `-exec/-delete` 不安全，`sed -i` 不安全）。**保守**：不可解析/有副作用 → 非已知安全。
- **rule_matcher.py**：`parse_rule("Bash(git commit)", ...)`、`rule_matches`（tool 名支持 exact / `mcp__server` 命名空间 / glob fnmatch；pattern 对 permission-target fnmatch）。
- **rule_store.py** `RuleStore`：`resolve(tool,target)` 按**行为优先级 deny>ask>allow**（非 source 优先），故 deny 真正免 bypass；`add_session_rule`（用户选 "always" 时写入内存）。
- **engine.py** `PermissionEngine`：
  - 轴 A 11 步流水（免 bypass 优先）：deny 规则 → tool_check deny → ask 规则 → tool_check ask → bypass 模式 allow → allow 规则 → tool_check allow → acceptEdits&mutates_fs allow → plan deny → dontAsk deny → 默认 ask。
  - 轴 B 沙箱（仅作用于非用户新批准的 allow）：`SandboxGuard.check_write(path)`，越界则升级问用户（`build_escalation_prompt`），"always" → `add_session_root` 拓宽，无交互通道则 fail-closed。
  - `check_multi(targets)`：逐目标静态判定，**最严格胜**折叠（任一 deny→deny、任一 ask/escalation→合并单提示、否则 allow）。
- **sandbox/guard.py** `SandboxGuard`：`check_write`——full 全放/read-only 全拦/workspace-write 仅 cwd+writable_roots+session_roots 内（`_norm` 解符号链接做稳定包含判定）。
- **prompts.py**：`build_approval_prompt`/`build_escalation_prompt`/`parse_approval_response`（yes→once、always/session→session、其他→deny fail-closed）。

### 11. 后台任务系统（`tasks/`）
async-first 事件驱动框架，长任务（shell/coroutine/agent）并发跑而不阻塞 LLM loop。
- **types.py**：`TaskType`(SHELL/COROUTINE/AGENT)、`BgStatus`(PENDING/RUNNING/SUCCESS/FAILED/CANCELLED/TIMEOUT/WAITING_FOR_ROUTE)、`BgTaskResult`（后台工具立即返回类型，带 poll 协程/factory/graph_ref）、`TaskMeta`（完成后持久元数据）、`BackgroundTaskNotification`（推 msg_buffer 的结构化完成通知）。
- **pool.py** `BackgroundTaskPool`（每 session 一个）：`submit(coro,...)->task_id` 包 semaphore（默认并发 5）+timeout+可选 progress sink；`wait_any(timeout)->"task_done"|"new_message"|"timeout"`（供 `Role.wait_interruptible`）；`wait_for_completion`/`wait_all`/`cancel`/`adopt`/`resubmit`。一次性 future 广播设计（无 lost-wakeup）；`_on_done` 据异常定状态、构 `<task-notification>` XML 推 msg_buffer（`MessagePriority.NEXT`）。
- **attachment.py** `TaskAttachmentGenerator`：每 LLM cycle poll 跑中任务出增量（`_offsets` 游标 + `_notified` 防终态重报），`format_attachment_xml` → `<task-attachment>`。
- **disk_output.py**：`DiskTaskOutput`（per-task 非阻塞 append + 懒 drain loop + 字节上限）+ `TaskOutputStore`（task_id→output 注册表，默认 `.task_outputs/`）。
- **stall_detector.py** `StallDetector`：`PeriodicLoop` 监控输出停滞 + 交互提示正则（`(y/n)`/`Password:`），命中推 stall_warning。
- **promotion.py** `async_background`：前台跑 `foreground_timeout`(30s)，没完成则 adopt 进 pool 返 BgTaskResult（防 UI 卡）。
- **decorators.py**：`@bg_tool` 标记自动后台派发、`require_bg_complete`、`is_bg_tool`。
- **turn_context_source.py** `BackgroundTaskContextSource`（实现 `EphemeralContextSource`）：桥接后台任务到 per-turn bus，pool 出现后只建一次 generator（保游标状态跨 cycle）。
- **bggraph/**（langgraph 风声明式 DAG）：

  **解决什么问题**：单个后台任务（`pool.submit` 一个 coroutine）适合「跑完即报」的原子长活，但复杂命令（如「调研→起草→渲染→合成」流水线）天然是**多阶段、有依赖、可并行、需中途决策**的。把这种流程硬写进一个大协程，会失去阶段级的可观测性（哪一步在跑/失败）、阶段级重试、以及「跑到一半停下来问 LLM 该走哪条路」的能力。bggraph 就是为「把一条多阶段命令声明成图，交给框架调度」而生——构图后 `compile()` 成一个普通后台任务塞进 pool，复用既有的并发/落盘/通知/中断基建。

  **为什么是 langgraph 的「转移」模型而非静态拓扑 DAG**：核心理念是边代表**激活（transition）而非静态依赖**——源节点一完成就立即触发目标，不必等整层就绪。这样才能表达三件静态 DAG 表达不了的事：① **环**（节点可被重新激活，如「评审不过→打回重写」回路），靠 `recursion_limit`（总激活次数）兜底而非编译期禁止；② **动态路由**（`add_conditional_edges` 的 router 读完成后的 state 决定下一跳，可指回上游成环）；③ **AND-join**（`add_edge(["a","b"],"c")` 多源汇合，只在所有源到齐才触发）。调度器因此是**前沿（frontier）super-step** 模型：维护一组在制 task，as-completed 收割、算后继、再 spawn，而不是预先排好的拓扑序。

  **LLM-in-the-loop（设计的点睛）**：`add_llm_edges` 让图**主动暂停**把决策权交还 LLM——节点完成后图取消前沿、返回 `LlmPauseResult` 快照（state+completed+触发边），并推一条 `waiting_for_route` 通知列出每个候选分支及其 `resume_tasks(from_node=...)` 调用方式；LLM 想清楚后再 resume 续跑。这把「后台自动流水线」和「Agent 推理」缝合：确定性的部分让图跑，需要判断的岔路口回到模型。

  **框架独占重试，节点零旋钮**：重试的「重试几次/退避多久」由 engine 统一拥有（`RecoveryRunner`，固定 3 次 + 指数退避带 jitter，仅瞬时失败消耗预算、permanent 错误快失败），节点本身**不暴露任何重试参数**。理念是让重试语义在全图一致，避免每个节点各写一套；消耗的重试次数记到异常上供上报。**并行节点独立失败**：一个挂掉只推即时通知、其余前沿继续跑，终态再把所有失败汇总成一条 `GraphBatchFailureError`，而非一处失败炸全图。

  **可观测性**：每个节点的状态迁移（running/success/failed/retrying/skipped）都经 `report.py` 的 progress contextvar 落盘（真相源）并镜像上事件总线；`notify.py` 把生命周期渲染成结构化通知，再经既有 `TaskAttachmentGenerator` 以 `<delta-summary>` 浮现给 LLM——所以 LLM 始终能「看见」后台图跑到哪、哪步失败、卡在哪个路由口，而不是只等一个最终结果。

  **实现落点**：`types.py`（`Stage`/`GraphState`/各边定义，依赖极薄供 pool 引哨兵）；`graph.py` `BgGraph`（`@node` 装饰 + `add_edge`/`add_conditional_edges`/`add_llm_edges` + `compile()` 校验 + `stage_summary` 分层摘要 + `resume`/`resume_skip` 续跑）；`engine.py` 前沿调度器；`report.py` progress 原语；`notify.py` 通知模板。

### 12. MCP 集成（`mcp/`）
协议驱动的工具发现与执行：连接 MCP server → 运行时发现工具 → 包成 `BaseTool` 注册进 executor → 走统一派发。
- **client/**：`MCPBaseClient`（抽象基，`list_tools`/`call_tool` 带 tenacity 重试 `retry_if_retryable_error` + ExceptionGroup 解包 `extract_meaningful_error`）；`MCPStdioClient`（`stdio_client` 子进程 + `AsyncExitStack` 懒 session）；`MCPSSEClient`（`enhanced_sse_client` 修 peer-closed 挂起）；工厂 `get_mcp_client(config)`。
- **universal.py** `UniversalMCP`：`initialize(server_names)` 拉 tool（命名空间 `server:tool`，支持 config 别名）；`call_tool(name,params)->json str`（懒建/缓存 per-server client）；`register_tools(executor)` 逐工具包 `MCPToolAdapter` 注册。
- **mcp_adapter.py** `MCPToolAdapter(BaseTool)`：运行时构造（故不 `@register_tool`），`call` 委托 `self._mcp.call_tool`，`native_schema` 直接透传 MCP `inputSchema`（已是 JSON Schema 无需签名自省）。
- **manager/manager.py** `MCPClientManager`（单例 `mcp_manager`）：线程安全延迟注册 `ensure_tools_registered`、per-server client 缓存、`_create_mcp_namespace`（"GitHub API"→"github_api"）。
- **mcp_registry.py** `ToolRegistry`（pydantic，`TOOL_REGISTRY`/`MCP_REGISTRY` 单例）+ `tool_data_type.py` `Tool` DTO（name/schemas/session_aware/instantiable）。

### 关键设计模式
- **能力注入而非反射**：工具声明 `requires`，bind 仅按 `Role.tool_capabilities()` 白名单注入命名属性，触不到 Role 内部。
- **单派发咽喉**：静态+MCP 工具同 `_tools` map，`run_command` 串起校验/权限/事件/恢复/限流。
- **结构化失败信号**：纯返回值永远成功，失败必须 raise ToolError 或 return `ToolResult(success=False)`。
- **免 bypass 安全约束**：deny/ask 规则与 tool_check 穿透 bypass 模式，适合硬安全红线。
- **双轴正交权限**：审批模式与沙箱独立组合（规则放行但沙箱升级、或 bypass 放行但沙箱拦截）。
- **大结果落盘而非截断**：`<persisted-output>` 保指针+首段，`result_id` 幂等。
- **自包含注册表**：装饰器 + 白名单扫描自动发现，无中央 import 清单。

---

## metagpt.router —— LLM 路由与多 provider 接入

router 是统一的 **LLM 模型选择 + provider 抽象层**，替代旧 factory。三件事：(1) 把多家 LLM 协议（OpenAI 兼容 / 原生 Anthropic）抽象成统一 `BaseLLM` 接口并自动选 client；(2) 提供三种路由方式（显式 / task-map / 智能策略）选模型；(3) 串起成本核算、OAuth 凭证、错误恢复、上下文压缩。严守分层：router 只依赖 `common.*`，永不 import context/ 及以上。

### 目录与分层
```
router/
├── router.py        # LLMRouter 主编排（注册模型卡 / 三路由方式 / 实例缓存）
├── schema.py        # ModelCard / RoutingRequest / RoutingDecision（纯 pydantic 信号载体）
├── strategy.py      # RoutingStrategy ABC + RuleBased / Complexity / LLMJudge / Squilla 策略
├── complexity.py    # 任务复杂度评分（LOW/MEDIUM/HIGH 分档，opensquilla 移植）
├── flags.py         # 五路由 flag（high_risk/debug/repo_arch/strict_format/long_context）
├── control.py       # RouterControlHold 会话级钉模型（TTL + turn 预算）
├── squilla.py       # opensquilla 全流水（ML 推理 ⊕ 启发式 fallback + 后处理）
├── llm/             # provider 抽象（BaseLLM / OpenAILLM / AnthropicLLM / registry / recovery / 压缩）
├── ml/              # LightGBM⊕MLP 集成推理（390 维特征 → R0-R3 route class）
├── oauth/           # OAuth 2.0 运行时（auth_code+PKCE / device_code / 多后端 token 存储）
└── cost/            # token 用量 + USD 计价核算（见前述 cost 子系统）
```

### 1. LLMRouter（`router.py`）—— 三路由方式
持 `_cards`（ModelCard 注册表）+ `_instances`（已建 BaseLLM 缓存）+ `strategy` + `task_map`，Context 懒依赖（模块加载零硬 import）。
- `_auto_register_from_config()`：自动发现 config 上每个 `LLMConfig` 字段（字段名即模型名，"llm" 为默认）。
- **方式 1 显式** `route(*, name, llm_config)`：给名字或 config 直接返回 LLM。
- **方式 2 task-map** `route_for_task(task)`：task_map 查表，落 default。内置 `COMPRESSION_TASK`（ContextManager 自动压缩用 compress_llm）、`SUMMARY_TASK`（end_session 摘要用 summary_llm）。
- **方式 3 智能** `async aroute(request, *, candidates)` / `aroute_decision(...)`：跑 strategy 返回 LLM（+ RoutingDecision）。
- `_build(card)`：懒构造，支持 fallback（确定性拒绝 → 路由到下一个注册模型）；`make_fallback_supplier(*, exclude)` 出无状态供给器（FALLBACK 恢复用）。
- 模块级 `LLM(llm_config, context)`：旧 factory 的 drop-in 替代。

### 2. 路由数据模型（`schema.py`）—— 纯信号载体（逻辑全在 strategy/router）
- `ModelCard`：`name`/`llm_config`/`description`（LLMJudge 提示用）/`tags`/`tier`（0 廉价 ... 3 最强）/`context_window`/`supports_vision`。
- `RoutingRequest`：`text`/`messages`（messages 优先）/`estimated_tokens`/`requires_vision`/`requires_pdf`/`prefer_cheap`/`flags`（high_risk/debug/long_context/repo_arch/strict_format）/`session_key`；`token_estimate()`/`prompt_text()`。
- `RoutingDecision`：`name`/`confidence`/`source`（explicit/task/rule/complexity/llm_judge/squilla）/`fallback`/`reasons`/`tier`/`extra`（策略元数据）。

### 3. 路由策略（`strategy.py`）—— 可插拔
`RoutingStrategy` ABC：`async select(candidates, request, *, default) -> RoutingDecision`。运行时可换。
- **RuleBasedStrategy**（默认，确定性零 LLM）：优先级首匹配——vision/PDF → 视觉卡；长上下文（≥32K 或 flag）→ 窗口够且 tier 高的卡；high_risk/debug → 最强卡；prefer_cheap → 最低 tier；无强信号 → default。
- **ComplexityStrategy**（确定性）：prompt → 抽信号 → 加权分 → LOW/MEDIUM/HIGH 档 → 插值映射到 tier 梯。
- **LLMJudgeStrategy**（额外 LLM 调用）：列候选 name/desc/tags/tier 让 LLM 选名（exact 0.9 / substring 0.7 / fallback 0.5）。
- **SquillaStrategy**（capstone，见 squilla.py）。

### 4. 复杂度评分（`complexity.py`）—— opensquilla 移植
三类信号 dataclass：`LexicalSignals`（词数/文件路径数/代码块/architecture·debugging·simple·risk 关键词/question_depth/隐含需求）、`StructuralSignals`（子任务数/跨文件/测试要求/domain 专精/可逆性/影响范围）、`ContextSignals`（历史失败数/对话轮次）。`complexity_score(signals)` 加权求和 → `score_to_tier`（<4 LOW，4-8 MEDIUM，≥8 HIGH）；`RoutingRule`（lambda 条件 → tier override，仅升不降）；`extract_all_signals`/`signals_from_messages`。

### 5. flags（`flags.py`）+ control（`control.py`）
- `RoutingFlags`(dataclass) + `compute_flags(text, *, context_tokens_est)`：关键词/模式 + 长度启发（≥6000 字符 / 代码块 ≥1500 / ≥2 文件引用 / context_tokens>2000 → long_context）；`merge_request_flags` 与 caller flag 并集（仅升级）。
- `RouterControlHold`/`RouterControlHoldStore`（in-memory session 级）：operator 把路由钉到某模型，按 idle TTL（默认 600s）或 turn 预算过期（monotonic 时钟免时钟漂移）；`build_control_targets`/`resolve_control_target`（接受 "model:NAME" 或裸名）。

### 6. squilla 全流水（`squilla.py`）
`SquillaStrategy` 编排 ML 推理或启发式 fallback → 统一后处理 → 终判：
- Step0 router-control hold 优先；Step1 ML 推理（LightGBM⊕MLP）或启发式（complexity score → `score_to_probs` 高斯桥），两路汇合于同一 `apply_postprocess`；Step2 caller flags floor；Step3 终判（置信度门 < 0.5 落 default_route_class、`detect_complaint` 用户抱怨升级、KV-cache 防降级、大上下文 floor R2/R3）；Step4 调和 thinking_mode + prompt_policy；Step5 R0-R3 映射到候选 tier 梯并记历史。`RoutingHistoryStore`（per-session 时间窗历史，防降级用）。

### 7. provider 抽象层（`llm/`）
- **BaseLLM**（`base_llm.py`）：统一接口。消息构造 `_user_msg`/`_user_msg_with_media`（多模态图片+document，PDF 限 15MB/80 页）/`_system_msg`/`format_msg`/`_build_messages`；高层 API `async aask(...stream=True)`（纯文本）/`aask_tool(...tools, tool_choice, stream=True)->LLMResponse`（文本+结构化 tool calls）/`aask_batch`/`aask_code`；provider 实现 `_achat_completion`（阻塞）/`_achat_completion_stream`（文本流）/`_achat_completion_stream_tool`（工具流，默认降级阻塞）；恢复编排 `_run_with_recovery`；归一 `get_choice_text`/`get_choice_tool_calls`（wire 无关）；成本 `_update_costs(usage)`→`TokenUsage.from_usage`→`cost_manager.add`；`compress_messages` 委托 RequestContextBuilder；`rotate_credential`（默认 no-op）。
- **OpenAILLM**（`openai_api.py`）：`@register_provider([OPENAI, FIREWORKS, OPEN_LLM, ...])`。`_cons_kwargs`（gpt-5 去 temperature/max_tokens、claude-opus-4-8 去 temperature）；流式工具按 index 累 tool_calls 重建 ChatCompletion；`_repair_tool_arguments`（json_repair 兜底坏 JSON）；OAuth `_build_oauth_manager`；`rotate_credential` 轮 key / refresh OAuth。还有 moderation/tts/stt/gen_image。
- **AnthropicLLM**（`anthropic_api.py`）：`@register_provider([ANTHROPIC])`，base_url 含 anthropic.com 自动选。核心 `_convert_messages`（OpenAI wire → Anthropic：system 抽顶层、tool→tool_result block、assistant tool_calls→tool_use block、合并连续同 role 保交替）；`_convert_tools`/`_convert_tool_choice`；`messages.stream()` ctx 流式，`get_final_message()` 含 text+tool_use blocks 直接归一。max_tokens 必填。
- **registry**（`llm_provider_registry.py`）：`@register_provider(keys)` 单例注册 + `resolve_api_type(config)`（api_type==ANTHROPIC 或 base_url 含 anthropic.com → ANTHROPIC，否则原 api_type；故 Claude 经 OpenAI 网关仍走 OpenAI client）+ `create_llm_instance`。
- **provider_catalog.py**：品牌预设（`ProviderPreset{base_url, api_type, env_keys, default_model, oauth_provider}`）——openai/anthropic/deepseek/moonshot/fireworks/open_llm/groq/xai/... 填 LLMConfig 未设值。
- **LLMResponse**（`llm_response.py`）：`LLMToolCall{id,name,arguments:dict}` + `LLMResponse{content, tool_calls, has_tool_calls}`，统一 OpenAI function call 与 Anthropic tool_use。
- **recovery**（`recovery.py`）：`build_llm_strategies(compress, rotate, fallback, on_fallback, transformers)`→`{RecoveryAction→async strategy}`（COMPRESS 重压缩 / ROTATE_CREDENTIAL 换 key / FALLBACK 换 provider / transformers）；能力为 None 的 action 省略（loop 改 re-raise）。RETRY/ABORT 由 tenacity 处理。
- **transformers**（`transformers.py`）：请求修复——`shrink_image`（PIL 缩 4MB 内）/`downgrade_tool_content`（tool 消息 list→纯文本，某些网关只收 string）/`strip_request_state`（剥非规范 key/thinking block，换 provider 用）。
- **request_context_builder.py** + **editor_read_parser.py**：每调用前压缩消息进窗口；`BALANCED_CUT_BY_TOKEN`（Phase1 每条 editor.read 压到 0.5×保 cache 稳定，Phase2 超均值按比例压，Phase3 保新弃旧），多模态保 image/document block。
- **context.py** `Context`：`config + cost_manager`；`_select_costmanager`（FIREWORKS→FIREWORKS mode / OPEN_LLM→FREE / else 共享 STANDARD）；`llm()`/`llm_with_cost_manager_from_llm_config`。

### 8. ML 路由推理（`ml/`）—— LightGBM⊕MLP 集成
预测 R0-R3 route class（R0 最简 → R3 最复杂）+ 派生 thinking_mode(T0-T3)/prompt_policy(P0-P2)。模型 artifact 缺失则优雅降级到启发式，两路汇合于同一后处理保一致。
- **engine.py** `SquillaMLEngine`：懒加载（延后 import lightgbm/sklearn/onnxruntime），`available` 触发加载，`predict(request)->InferenceResult|None`（异常全捕获降级）。
- **特征 390 维**：8 通道——hand-crafted(51, 正则文本分析) + TF-IDF+SVD(102) + context(10, 会话态) + history(16, 含 trajectory 8 one-hot) + BGE×3 通道 PCA(192, 当前/历史/上条助手) + assistant handcrafted(12) + continuation(2) + reasoning(5)。
- **inference/**：`artifacts.py`（manifest 校验 feature_dim=390/mlp_input_dim=1536/temperature/per_class_alpha；加载 lgbm/tfidf/svd/bge_pca/mlp onnx）；`heads.py`（LightGBM main + 可选 aux「升降意图」+ MLP ONNX 1536→logits 温度校准）；`ensemble.py` `fuse_probabilities`（per-class alpha 加权混合）；`postprocess.py` 6 层（argmax→route+margin、低 margin 升级、aux 降级 KV-cache、R1 救援、under-routing 安全 floor、flag override）。
- **bge_onnx.py** `OnnxBGE`：ONNX 跑 BGE 嵌入（pickle-safe），backend 可切 sentence_transformers / onnx(INT8 快)。
- **trajectory.py**：历史轨迹分类（COLD_START/STABLE_LOW/STABLE_HIGH/ESCALATING/DESCALATING/OSCILLATING/...）。
- 默认模型目录 `~/.metagpt/router_models/v4.2_phase3_inference/`；`router.runtime.yaml`（阈值/tier 映射/flag 规则）随模块 vendored，无权重也能跑后处理。

### 9. OAuth 运行时（`oauth/`）
OpenAI 兼容 provider 的 OAuth 2.0：多 grant（auth_code+PKCE / device_code / client_credentials / refresh_token）、多 provider（openai/anthropic/github-copilot 预设，**不内置 client_id/secret** 防冒充）、多存储后端、跨进程刷新协调。
- **models.py**：`AuthMode`/`TokenClaims`/`DeviceCodeInfo`/`OAuthToken{access_token, refresh_token, expires_at, scopes, claims; is_expired(buffer)}`。
- **registry.py** `PROVIDER_PRESETS`：openai/anthropic（`anthropic-beta` 头）/github-copilot（device_code），`apply_preset`（用户值优先，headers_extra 合并）。
- **manager.py** `OAuthManager`：`get_valid_token()`（快路服缓存/慢路 filelock 重读再 mint）/`force_refresh()`（401 时 rotate_credential 调）/`login(callbacks)`（按 grant 分派 auth_code/device_code flow）。lock 文件 `{CONFIG_ROOT}/oauth/{provider}.lock` 跨进程协调。
- **client.py** `OAuthClient`（同步 httpx）：`client_credentials`/`refresh`/`exchange_code`（PKCE）/`request_device_code`/`poll_device_token`（处理 authorization_pending/slow_down）/`revoke`。
- **storage/**：`CredentialStore` ABC + `FileCredentialStore`（`~/.metagpt/oauth/{provider}.json` 0600 原子写）+ `KeyringCredentialStore`（OS keyring）+ `FallbackCredentialStore`（keyring 优先降级 file，Claude Code 法）；`get_store(provider, backend)` 工厂。
- **flows/**：`run_auth_code_flow`（起 localhost server 收 `?code&state`，校 state 防 CSRF，超时 300s）/`run_device_code_flow`（RFC 8628 无浏览器，github copilot 用）；`LoginCallbacks`（on_url/on_device_code/on_progress）。
- **jwt_utils.py**（`decode_jwt_payload` 不验签——TLS 端点已可信、客户端无密钥、只读 exp 主动刷新）+ **pkce.py**（`gen_code_verifier`/`gen_code_challenge` S256/`gen_state`）+ **redact.py**（敏感参数→`***` 安全日志）+ **errors.py**（`classify_refresh_failure` 标 recoverable，invalid_grant 等不可恢复）。

### 10. cost 核算（`cost/`）
统一 token 用量 + USD 计价（吸 Claude Code per-model ModelUsage + Codex context-remaining + 旧 CostManager 兼容）。
- **usage.py** `TokenUsage`（input/cached_input/cache_creation/output/reasoning/total）：`non_cached_input`/`blended_total`；适配器 `from_openai`/`from_anthropic`/`from_usage`（自动识别 provider shape）。
- **pricing.py** `ModelPricing`（per-Mtok input/output/cache_write/cache_read）；`PRICING`（桥 TOKEN_COSTS + 显式 claude/opus/sonnet/haiku 分层）；`lookup_pricing`（精确→最长包含匹配）；`DEFAULT_UNKNOWN_PRICING`（Sonnet 档，未知模型不丢成本）；`PricingMode`{STANDARD/FIREWORKS/FREE}；`cost_of(usage, model, mode)`。
- **tracker.py** `CostTracker`：`add(usage, model)->cost`（零用量 no-op）；`model_usage` per-model 桶；兼容 `update_cost`/`get_costs()->Costs`；`context_remaining(model)`（Codex 风，BASELINE_TOKENS=12000）。
- **report.py**：`format_cost`（≥$0.5 两位否则四位）/`format_total_cost`（CC /cost 块）/`final_output`（Codex 单行）/`status_line_dict`。

### 关键设计模式
- **三路由方式并存**：显式 / task-map / 智能策略，策略可插拔运行时换。
- **wire 协议而非模型名选 client**：`resolve_api_type` 按端点选 OpenAI/Anthropic client，归一接口对调用方透明。
- **优雅降级链**：ML 推理失败 → 启发式 fallback；keyring → file token 存储；流式工具 → 阻塞。
- **恢复透明**：`_run_with_recovery` 串 COMPRESS→ROTATE→FALLBACK，调用方不感知重试。
- **session 级有状态**：路由历史 / control hold / OAuth token 按 session_key/provider 隔离。

## metagpt.context —— 会话历史与上下文管理（记忆与压缩）

负责「LLM 看到什么」：消息存储的增删改查、历史的压缩编排（microcompact + autocompact）、技能注入、以及 per-turn 易变上下文的临时注入。是 think 侧组装 prompt 的数据底座。

### 目录结构
```
context/
  manager.py            # ContextManager —— 消息存储 CRUD + 历史编排门面
  token_budget.py       # token 计数 + 预算评估 → TokenState
  microcompact.py       # 原地折叠旧工具结果（无 LLM）
  autocompact.py        # LLM 摘要压缩（保留尾部）
  prompt.py             # 压缩用 prompt 模板
  skills/               # 技能（slash-command 式可注入指令包）
    skill_definition.py # SkillDefinition（slug/description/always_apply/globs/instructions）
    skill_pool.py       # SkillPool（按名加载 + 扫描内置目录）
    skill_manager.py    # SkillManager（ensure_ready / reload 热替换）
    skill_injector.py   # SkillInjector（构建索引 + always_apply + 加载指引）
  turn_context/         # per-turn ephemeral 上下文统一注入层
    format.py           # wrap_system_reminder
    bus.py              # TurnContextBus（按 priority 并发收集）
    sources/            # git / token_pressure / compaction / lsp 各源
```

### ContextManager（manager.py）
- **职责**：消息存储的 CRUD 门面 + 历史压缩编排。历史背靠 `RoleState.context`（messages list 为真相源，ContextManager 不另存一份）。
- **事件发射**：`add()`→`MessageAppendedEvent`；压缩走 `PreCompactEvent`/`PostCompactEvent`；检查点 `CompactionCheckpointEvent`。
- **`manage_history()`**：先 microcompact（原地折叠）再 autocompact（LLM 摘要），是历史压缩的总编排入口。
- **`prepare_request(user_prompt, manage=True)`**：组装一次请求的历史（manage=True 时先跑压缩）。
- **熔断**：连续压缩失败计数，触发后停用 autocompact 防雪崩。
- **注入点**：构造期注入 `SessionRecorder`（Protocol）做 rollout 落盘；`add`→record_message，compacted→record_compaction。

### token 预算（token_budget.py）
- `count_tokens` / `context_window` / `effective_window` / `autocompact_buffer` / `autocompact_threshold`。
- `evaluate()->TokenState`：标志位 `above_warning` / `above_error` / `above_autocompact` / `at_blocking_limit`，外加 `percent_left`。供 TokenPressureContextSource 与 autocompact 触发判断共用。

### 两级历史压缩
- **microcompact.py（原地、无 LLM）**：`COMPACTABLE_TOOLS` frozenset 圈定可折叠工具；`keep_recent=5`、`trigger=10`——超过 trigger 条旧工具结果时，把除最近 keep_recent 外的工具输出原地清空（content 置为占位），保留消息骨架。返回 `MicrocompactResult`。零 LLM 调用、零网络、最省。
- **autocompact.py（LLM 摘要）**：把历史 head 用 LLM 摘要成一条 summary，preserve tail（最近若干轮原样保留）。返回 `AutocompactResult`（含 replacement_history + summary），与 ContextManager swap 历史一致。带熔断防止失败循环。
- **prompt.py**：`get_compact_prompt` / `get_partial_compact_prompt` / `format_compact_summary` / `get_compact_user_summary_message`，是 autocompact 喂给 LLM 的模板。

### skills/ —— 可注入指令包
- **SkillDefinition**：`slug` / `description` / `always_apply`（始终注入）/ `globs`（文件匹配触发）/ `instructions`（正文）。即 slash-command 风格的领域知识包。
- **SkillPool**：`load_by_names` 按名加载、扫描内置技能目录，是技能定义的容器。
- **SkillManager**：`ensure_ready` 惰性就绪、`reload` 热替换（改技能文件无需重启）。
- **SkillInjector**：组装注入内容——构建技能索引 + always_apply 技能正文 + 「如何加载更多技能」的指引段。

### turn_context/ —— per-turn ephemeral 注入层
- 设计：易变的 per-turn 信息（git 状态 / token 压力 / 压缩通知 / LSP 诊断）作 `<system-reminder>` 包装注入 **user prompt**，request-only——不进 history、不进可缓存的 system prefix。
- **format.py `wrap_system_reminder(blocks)`**：strip 空块，非空才包 `<system-reminder>…</system-reminder>`。
- **bus.py `TurnContextBus(sources)`**：构造按 `priority` 升序排；`async collect(*, cwd)` 用 `asyncio.gather` 并发 render 所有源，逐源 try/except 隔离（单源抛只 warn 跳过），drop 空/非 str 块，合并包装。
- **sources/**（均实现 `common/interface/turn_context.py` 的 `EphemeralContextSource` Protocol）：
  - `GitContextSource`（priority 10）：await `common.git_state.collect_git_state`→`render_git_section`。
  - `TokenPressureContextSource`（priority 20）：duck-type `provider.token_state()`，仅 `above_warning` 才发 "# Context budget" 块。
  - `CompactionNoticeContextSource`（priority 25）：one-shot，订阅 `PostCompactEvent`，压缩刚发生的下一 turn 提示一次。
  - `LspContextSource`（priority 40）：duck-type `service.drain_diagnostics()`。
  - 注：`BackgroundTaskContextSource`（priority 30）因需 import tasks 层，实现放 executor/tasks 层（守分层），但实现同一 Protocol。

## metagpt.think —— 思考引擎（think 侧）

ReAct 循环的「想」：把历史 + 系统 prompt + 工具 spec 组装成一次 LLM 请求并异步执行，产出 `ThinkResult`（文本 + 工具调用）。

### 目录结构
```
think/
  think_engine.py       # ThinkEngine —— 后台 asyncio.Task 跑一次 LLM 调用
  prompts/
    prompt_builder.py    # PromptBuilder（无状态组装 system/user prompt）
    inputs.py            # ThinkInputs / ThinkSubsystems / ThinkContext
```
（`ThinkResult` 在 `common/schema/think.py`，`BaseThinkEngine` ABC 在 `common/base/think_engine.py`。）

### ThinkEngine（think_engine.py）
- **ThinkResult**（common/schema）：`content` / `tool_calls` / `is_native` / `is_empty`。是 think 侧统一产物，屏蔽 XML/native 协议差异。
- **BaseThinkEngine**（common/base）：`start` / `join` / `done` 三态接口。
- **ThinkEngine**：`start(req, system_prompt, state_data, tool_specs, llm)` 起一个后台 `asyncio.Task` 跑一次 LLM 调用；`join()` 等结果；`done` 查状态。protocol-aware 去重——native（tool_calls）与 XML（文本）两种产物归一。background task 化便于 loop 侧并发观察与中断。

### prompt 组装（prompts/）
- **ThinkInputs**：扁平快照（cwd / platform / model / 各类静态串）——可缓存、稳定。
- **ThinkSubsystems**：活体协作者引用（turn_context_bus、command_channel 等）——每 turn 拉取新值。
- **ThinkContext**：二者组装后的完整上下文，喂给 PromptBuilder。
- **PromptBuilder（无状态）**：
  - `build()`：产出 system + user prompt。
  - `collect_context()`：拉齐各源（含 `await _make_reminders(bus, cwd)` 走 TurnContextBus）。
  - `_make_env_section()`：静态 `# Environment` 段（cwd/platform/model），git 已迁出到 turn_context 不再在此。
  - `_make_reminders()`：async，委托 `bus.collect(cwd=...)`。
  - **缓存边界**：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY`——之上静态可缓存，之下（reminders）每 turn 易变不缓存。

## metagpt.parser —— 命令协议通道（act 与 think 之间的翻译层）

把 LLM 输出解析成统一的命令中间表示（command IR），并屏蔽 XML 与 native tool-use 两套协议差异；同时反向提供 tool spec / command guide / hint 给 prompt。

### 目录结构
```
parser/
  native_channel.py     # NativeToolChannel（native tool-use 协议）
  xml_channel.py        # XmlCommandChannel（XML <end></end> 协议）
  factory.py            # make_command_channel
```
（`CommandChannel` ABC 在 `common/base/command_channel.py`。）

### CommandChannel ABC（common/base）
统一接口：`output_format` / `command_guide` / `command_hint` / `tool_specs` / `iter_commands` / `record_turn` / `turn_signature` / `is_terminal`。一套接口两种实现，think/loop 侧不感知协议。

### NativeToolChannel（native_channel.py）
- provider 为 `"openai"` 或 `"anthropic"`，决定 tool-spec envelope 形状。
- `get_native_tool_specs`：产出对应 provider 形的 specs。
- **`infer_native_tool_provider`**：按 `resolve_api_type` 的 **transport（wire 协议）**选 envelope，而非按模型名——Claude 经 OpenAI 网关时仍用 OpenAI envelope（修过的关键 bug）。
- `is_terminal`：`tool_calls` 为空即终止本 turn。

### XmlCommandChannel（xml_channel.py）
- `OUTPUT_SECTION` / `XML_COMMAND_GUIDE` / `XML_COMMAND_HINT`：教模型 XML 命令格式与 `<end></end>` 收尾语义。
- `parse_commands2`：解析 XML 命令块成统一 IR。
- `is_terminal`：识别 `<end></end>` 标记判定 turn 结束。

### 统一命令 IR
两通道都产出 `{command_name, args, id, status, error_msg}` 形的命令对象；`make_command_channel` 工厂按协议名建对应通道。command_guide/command_hint 按协议门控（native 返回 tool-call 语义不含 `<end>`，XML 返回原 XML 文本）。

## metagpt.loop —— ReAct 主循环（编排 think ↔ act）

驱动 observe→think→act→finish 的核心循环，串起 think 引擎、命令通道、执行器、记忆与上下文。

### 目录结构
```
loop/
  react_loop.py         # ReActLoop —— observe/think/act/finish 循环实现
```
（`LoopContext` 与 `BaseLoop` ABC 在 `common/base/loop.py`。）

### LoopContext（common/base）
配置载体：`max_react_loop` / `max_consecutive_react_limit` / `memory_k` / `msg_buffer` / `watch` / `observe_all`。

### ReActLoop（react_loop.py）
- **scatter 注入**：构造期接收一组**可调用/协作者**而非整个 Role——`think_engine` / `command_channel` / `executor` / `memory` / `context_provider` / `is_active` / `set_active` / `get_bg_pool`。守分层（loop 不 import Role），便于测试替身。
- **循环阶段**：
  - `_observe`：拉取 mailbox / watch 的新消息。
  - `_step_think`：起 think 引擎、await 结果。
  - `_step_act`：把命令 IR 交执行器跑，回填结果。
  - `_finish`：收尾、产出最终回复。
  - `run`：编排上述阶段，受 `max_react_loop` 等约束。
- **protocol-aware termination**：经 `command_channel.is_terminal` 判定终止，XML（`<end>`）与 native（空 tool_calls）统一处理。

## metagpt.memory —— 程序化记忆（待实现）

> 当前为占位/待实现状态，仅列出包结构。

```
memory/
  procedural_memory/
    decorator.py
    manager.py
    schema.py
    context_builders/    # action_node / base / role_zero / simple
    perfect_judges/      # base / simple
    scorers/             # base / simple
    serializers/         # action_node / base / role_zero / simple
  episodic_memory
  semantic_memory
```
- **严守分层**：router 只依赖 common，cost/oauth/catalog 永不向上 import。

---

## 全链路：一次 turn 的运行时数据流

前面按包静态拆解，这里把它们串成一次 turn 的动态流程（`environment`/`cli` 摄入一条 user prompt → 产出一条回复），看清各层如何协作。

```
用户输入
   │
   ▼
[environment] control.send_input → 调度器在 turn 边界 drain 邮箱推入 msg_buffer
   │
   ▼
[roles] Role.run()
   ├─ bind_trace(session_id) + set_bus(event_bus)        ← 全程日志关联 + 深层调用点可发事件
   ├─ 发 SessionStartEvent（首次，写 rollout 首行）
   ├─ 发 UserPromptSubmitEvent                            ← [hook] 可注入上下文 / 中止
   └─ loop = _make_loop() → await loop.run()
        │
        ▼
[loop] ReActLoop：observe → think → act → finish（按 max_react_loop 约束循环）
   │
   ├─(observe) 拉 mailbox / watch 的新消息
   │
   ├─(think) [roles.context_provider] prepare() →
   │     [think.PromptBuilder] 组装 system(静态可缓存前缀) + user
   │        └─ user 末尾追加 [context.turn_context] bus.collect() 的 <system-reminder>
   │           （git / token 压力 / 压缩通知 / 后台任务 / LSP 诊断，不进 cache、不进 history）
   │     [context.ContextManager] prepare_request() → manage_history()
   │        └─ microcompact(原地折叠旧工具结果) → autocompact(LLM 摘要，带熔断)
   │     [think.ThinkEngine] start() 起后台 asyncio.Task →
   │        [router.LLMRouter] 选模型/provider →
   │        [router.llm.BaseLLM] aask_tool(stream=True) →
   │           ├─ 流式 token 经 LLMStreamDeltaEvent 实时喂屏幕
   │           ├─ [router.cost] TokenUsage.from_usage → CostTracker.add 计费
   │           └─ [router.recovery] COMPRESS / ROTATE_CREDENTIAL / FALLBACK 透明恢复
   │        → ThinkResult(content + tool_calls)
   │
   ├─(act) [parser.CommandChannel] iter_commands() 把 ThinkResult 解析成统一命令 IR
   │        （native: 读结构化 tool_calls / xml: 解析 <end> 块）
   │     [executor.ToolExecutor] run_command() 单咽喉，逐命令：
   │        参数校验 → PreToolUse 事件([hook] 改参/拒绝) → 权限闸(审批轴+沙箱轴, deny/ask 免 bypass)
   │        → 恢复循环(瞬时失败重试) → tool.call() → 结果归一 → PostToolUse 事件
   │        → FileMutatedEvent(改盘工具 → [roles.lsp] 诊断同步 + [environment] 文件监视抑制回声)
   │        → 结果限流(超阈落盘换 <persisted-output>)
   │     工具结果回填历史，进入下一轮或终止
   │
   └─(finish) [parser] is_terminal 判定（空 tool_calls / <end>）→ 收尾产出回复
        │
        ▼
[roles] turn 边界：发 TurnEndEvent → [hook] 触发 Stop；去激活；回复打 display_name 标签
        │
        ▼
[environment] 回复发布到 env（或本地 buffer）→ 等待下一条输入
```

### 贯穿全程的两条横切脊柱

- **事件总线（`common/events`）**：`MessageAppended` / `LLMStreamDelta` / `FileSnapshot` / `CompactionCheckpoint` / `TurnEnd` 等事件由**同一事件流**同时喂「屏幕（renderer 订阅者）」与「磁盘（`session` 的 `RecorderSubscriber`/`FileSnapshotRecorder`）」，保证二者不发散。control 类事件（PreToolUse/UserPromptSubmit/PreCompact…）还会折叠 `HookOutcome` 反向影响宿主。
- **持久化（`session`）**：本 turn 的每条消息、每次压缩检查点、每个文件 before-image 都追加进 `rollout.jsonl`。崩溃后 `replay()` 单次正向扫描即可重建「历史 + 身份」（`compacted` 检查点自包含完整历史），`fork()` 可从任一会话派生血缘子会话。**配置（RoleSchema）不入 rollout**，由 caller 或 `environment` 的 residency 互补存储。