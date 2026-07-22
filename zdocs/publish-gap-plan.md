# mote 对外发布 — 补充模块 gap 分析与落地计划

> 对比基线：`/home/longert/run_rollout/hermes-agent`（成熟对外发布框架：25+ 消息平台、
> 30+ 模型 provider、完整插件系统）+ `/home/longert/run_rollout/CopilotKit`（AG-UI 前端集成）。
>
> **核心结论**：mote 的核心引擎已非常强（executor chokepoint、事件总线、恢复框架、
> compaction、run_graph、sandbox、tool search、EffectLedger、LSP、OAuth），架构不输甚至
> 优于对方。缺的是**"对外发布"所需的外围生态与集成层**，而非核心能力。绝大多数补充都能
> 挂在 mote 已有的自注册 seam（`@register_consumer` / `@register_tool` / `@register_provider`）
> 之后，**零核心改动**。

---

## 关键洞察：ViewEvent 是中立协议脊柱

mote 的 `cli/contracts/view/events.py` `ViewEvent` 已经是 provider-中立、transport-中立的
事件脊柱，且与 AG-UI 事件、IM card 需求几乎一一对应。因此消息网关（点一）、AG-UI/CopilotKit
前端（点五）、ACP（点五）本质都是**同一个 ViewEvent 流的不同 transport**，不该各造一套。

| mote ViewEvent | AG-UI event | ACP (Zed) |
|---|---|---|
| `MessageBlockStarted/Delta/Completed` | `TEXT_MESSAGE_START/CONTENT/END` | text chunk |
| `ReasoningDelta` | reasoning/thinking | thinking chunk |
| `ToolCallStarted` | `TOOL_CALL_START/ARGS` | tool_call (kind/locations) |
| `ToolCallCompleted` | `TOOL_CALL_RESULT` | tool_call_update |
| `TaskProgress` | `STEP_STARTED/FINISHED` | progress |
| `QuestionAsked` | HITL callback | request_input |
| `ApprovalRequested/Decision` | `requiresApproval` flow | ToolCallApprovalRequest |
| `FileDiffBlock` | generative UI / diff | edit_approval (diff) |
| `UsageUpdated` | `STATE_SNAPSHOT`(usage) | — |
| turn/session（已在事件上） | `RUN_STARTED/FINISHED` + `threadId` | session load/resume/fork |

mote §2.4 的设计意图（"一个 ViewEvent 流喂 TUI 和 feishu card，无 `if consumer==` 分支"）
延伸到网络 transport 就是 AG-UI/ACP。

---

## 点一 — 消息平台网关（已预留，缺实现）

**现状**：`cli/consumers/` 已预留 `feishu/`、`web/`、`app_server/` 三个 stub；`registry.py`
的 `@register_consumer` + capability-downgrade 架构已就绪；stub docstring 已写明落地路径
（"add `feishu/consumer.py` … zero core changes"）。

**结论**：不缺架构，缺**实现 + 平台 transport（webhook 收发）**。与点五合并设计，因为
web/feishu consumer 和 AG-UI consumer 是同一 seam 的不同 transport。

**不做**：hermes 的 25 平台全量适配器。IM 私有能力（discord/spotify/homeassistant）应作为
可选 skill / MCP 存在，不进核心框架（违背单一控制面原则）。

---

## 点二 — Cron 定时任务（自主运行）

**复用**（不重造）：
- `common/scheduling/loop.py`（tick 循环原语）
- `environment/scheduling/`（task 抽象 + prompt 注入扫描 `task.py`）
- `environment/control.py`（session/turn/thread 生命周期，非交互 spawn + `timeout_seconds` 已有）
- `common/hook/`（PreToolUse gate → cron toolset 门控）
- EffectLedger（cron 崩溃重启对账，天生契合）

**新增**（最小面）：

| 模块 | 职责 |
|------|------|
| `common/scheduling/cron/schema.py` | `CronJob`（cron 表达式、prompt、enabled_toolsets、delivery target、enabled 标志） |
| `common/scheduling/cron/store.py` | Job CRUD + JSONL 持久化（`~/.mote/cron/jobs.jsonl`），复用 EffectLedger append-only 模式 |
| `common/scheduling/cron/scheduler.py` | tick（复用 `loop.py`）→ 选 due job → 文件锁串行化（fcntl/msvcrt）→ 非交互 spawn → 投递 |
| `executor/tools/cron.py` | `Cron` 工具：agent 自己增删查定时任务（对标 hermes `cronjob_tools.py`） |

**零负债要点**：
1. 非交互批准：`PermissionMode.CRON`（自动 allow + 禁用 `Human`/`AskUserQuestion`/`Cron` 防递归）——复用现有 permission 门。
2. toolset 门控：per-job allowlist 通过 `RoleSchema.tools` 交集 + hook deny——已有机制。
3. prompt 注入扫描：`common/utils/prompt_sanitizer.py` 已存在，组装后过一遍。
4. 投递：走点五设计的 consumer/channel（发回原 IM 会话），不单独造投递层。

**里程碑**：① schema+store+CRUD 工具（可测）→ ② scheduler tick+锁+非交互 spawn →
③ 接投递 → ④ 重启恢复（EffectLedger 对账）。

---

## 点三 — 多模态 Provider 生态（抽注册表）

**现状**（已比预期好）：
- `executor/tools/generate_media/creators.py` 已有 `ImageCreator/VideoCreator/AudioCreator/MusicCreator` + `generate_media_tool.py`
- `executor/tools/web_search.py` 支持 Exa/DuckDuckGo/Google
- `executor/tools/device_use.py`（computer/device use，Android over adb）已有

**真正 gap = 注册表广度，非架构**。hermes 每类能力一个 `*_registry.py` + `*_provider.py` 抽象基类，
后端按需插拔（web_search 8 后端、image_gen 5 后端）。mote 的 `creators.py` 把多后端**硬编码在一个文件**，
不是可插拔注册表。

**零负债建议**（对齐 mote 已有 `@register_*` 惯例）：
1. 抽 `MediaProvider` 注册表：`creators.py` 硬编码后端 → `@register_media_provider("flux", kind="image")` 自注册；creator 变成"选 active provider + dispatch"。加 Kling/Veo/ElevenLabs = 新文件 + 装饰器，零核心改动。
2. web_search 同样：`_search_engine.py` 已是 ripgrep 引擎共享层；给 web_search 建 `SearchBackend` 注册表，补 Tavily/Brave/SearXNG/Firecrawl。
3. TTS/transcription：mote 有 `AudioCreator` 但无独立 STT 注册表；若对标补 `transcription` provider 注册表。

**关键判断**：**不该发布前做全**。抽注册表（硬编码→装饰器）值得现在做（一次性零负债）；
具体后端需求驱动增量加。**只做"抽注册表"这一步**。

---

## 点四 — Memory（跳过）

`common/prompt/memory.py` 已有 prompt 契约但未实现存储层。本轮不做。

---

## 点五 — ACP / 前端集成（AG-UI，结合 CopilotKit）

**核心洞察**：不为 ACP 和 CopilotKit 各造一套。它们连同点一的 web/feishu 都是同一个
`ViewEvent` 流的不同 transport。**AG-UI 是开放标准**（不锁 CopilotKit）：实现 AG-UI consumer
即同时兼容 CopilotKit React SDK + 任何 AG-UI 客户端——比对 CopilotKit 私有 API 编程更零负债。

**CopilotKit 关键事实**（v2 runtime）：
- wire = **AG-UI events over SSE**（`text/event-stream`，单行 JSON）
- 端点：`POST /agent/:agentId/run`（SSE 流）、`/info`（agent 发现）、`/connect`（load state）、`/stop`
- 有状态：`threadId` 跨多轮维持会话（↔ mote `session_id`）
- 状态同步：前端→后端 `RunAgentInput`（含 messages+state）；后端→前端 `STATE_SNAPSHOT/DELTA`
- HITL：tool `requiresApproval` → 前端提示 → 回传
- **不用 GraphQL**（v1 已弃用），只走 v2 纯 SSE + `/info`

**最优设计（一个协议脊柱，N 个 transport adapter）**：
```
                ┌─ ViewEvent（已存在，中立脊柱）
Role/loop ─────┤
                └─ CapabilityAdapter（已存在，按 capabilities 降级）
                       │
   ┌───────────────┼──────┬───────────┬────────────┐
 terminal        web/AGUI      feishu      app_server      acp
 (已实现)    @register_consumer  (stub)    (proto stub)   (新)
```

1. **AG-UI consumer**（覆盖 CopilotKit + web + 通用前端）= `web` stub 落地：
   - `AguiConsumer(BaseConsumer)`：`ViewEvent` → AG-UI JSON，SSE 帧输出。
   - 薄 HTTP 层：`/agent/:id/run` POST→SSE、`/info` 发现、`threadId`↔`session_id`。
   - capabilities = `streaming=True, markdown=True`，复用现有 rich 流，零 `if consumer==` 分支。
   - HITL：`ApprovalRequested` → AG-UI approval 事件 → 前端回传 → 现有 `ApprovalDecision` 闭环。
   - STATE_SNAPSHOT：`UsageUpdated` + session 状态天然可投影。

2. **ACP consumer**（Zed）= 复用同一 `ViewEvent`→事件映射，transport 换 stdio JSON-RPC（非 SSE）。
   `FileDiffBlock` → ACP edit_approval 天然契合。有了 AG-UI 映射表后，ACP 几乎免费。

**零负债关键**：协议脊柱只有一个（ViewEvent），永不为某前端加分支；AG-UI 开放标准不锁厂商；
不引入 GraphQL。

**里程碑**：① `ViewEvent→AG-UI` 映射器（纯函数，可单测，无 transport，**核心资产**）→
② SSE HTTP 层 + `/info` + threadId↔session → ③ AguiConsumer 注册 → ④ HITL/approval 回传闭环 →
⑤ ACP consumer 复用①换 transport。

---

## 点六 — 发布打磨项（按 安全底线→体验→锦上添花 排序）

**A. 安全底线（发布前）**
1. **Skills 治理 + 审计**：mote 有 `skill_pool/manager/injector` 但缺来源治理。补
   `context/skills/audit.py`（AST 结构校验 + 密钥/注入扫描，对标 hermes `skills_ast_audit`+`skills_guard`）
   和 provenance（来源/版本/信任级）。理由：skills 是可执行 procedural memory，发布若允许安装
   第三方 skill 而不扫描 = 供应链风险。
2. **依赖 CVE 扫描**：`common/security/osv_check.py`（OSV 查询），可选，安装 MCP/skill 带依赖时触发。

**B. 体验（发布后跟进）**
3. **MCP OAuth 打通**：mote 有独立 MCP（`executor/mcp/`）+ 独立 OAuth（`router/oauth/`，含
   auth_code/device_code/pkce/keyring）——**两者未连**。补 `executor/mcp/oauth.py` 桥接：MCP server
   需 OAuth 时复用 `router/oauth/manager.py`。零新框架，纯接线。
4. **title_generator**：自动会话标题（辅助模型一次调用），落 session 层。小、独立。

**C. 锦上添花（需求驱动）**
5. **rate_limit_tracker**：追踪 provider 响应 `x-ratelimit-*` header，`/usage` 展示。mote 已有
   `router/cost/`（pricing/tracker/usage），这是补限流侧可见性，接在 `router/llm/base_llm` 响应处理。
6. **多源 credential pool**：mote 现有单 provider 内 key rotation（`credentials.py`
   `CredentialRotationMixin`）。若需跨源 failover（env/OAuth/device code 混合池），扩展该 mixin。
7. **onboarding**：首次运行引导。低优先级。

**不做**：hermes 25 平台全量、gamification、IM 私有工具——作为可选 skill/MCP 存在。

---

## 总体优先级

1. **点五 ViewEvent→AG-UI 映射 + web/AGUI consumer**（打通对外服务，顺带解锁 ACP、feishu，最高杠杆）
2. **点二 Cron**（自主运行）
3. **点六A 安全底线**（Skills 审计 + CVE，发布前）
4. **点三 抽 media/search provider 注册表**（一次性零负债重构，后端增量）
5. **点六B/C 打磨项**（MCP OAuth、title、rate-limit）

---

## 进度追踪

- [x] 点五：AG-UI / 前端集成（Phase 0-4 全量完成 + simplify + SinkConsumer 抽取；127 测试通过）
- [x] 点二：Cron 定时任务（命令式 CLI：`mote cron add/list/rm` + CronService 挂进 SessionDriver 生命周期；无 agent 工具/PermissionMode/sanitizer。753 测试通过）
- [x] 点六A：Skills 审计（`context/skills/audit.py` 纯 stdlib body 审计 → 注入/密钥/危险代码；`SkillPool._load_skill_from_dir` gate：CRITICAL 拒载、WARNING 记录。CVE/OSV 与 provenance 字段暂缓——当前无消费点，避免死债务）
- [x] 点三：media/search provider 注册表（对齐 LLM `@register_provider` 形态：`@register_media_provider(kind,name)` → `MediaProviderRegistry(Singleton)` keyed `(kind,name)` + `create_media_provider(kind,output_dir)` 读 `multimodal.{kind}_generation.provider`（默认 `openai`）；4 creators 抽成 `MediaProvider` 子类内建 `openai` 后端，TTS/transcription 走 `audio` kind 同一 pattern，未来 Flux/ElevenLabs 新增只需一文件+装饰器+config 改 provider。`@register_search_backend(name)` → `SearchBackendRegistry(Singleton)` + `create_search_backend(config,*,provider_search=)` 读 `tools.web_search.backend`（默认 `provider`，内建 `ProviderSearchBackend` 委托现有 `web_search` 能力）；Google 等厂商入口预留 config 可选。新增 `WebSearchConfig`（lazy-export）+ `ToolsConfig.web_search` + `multimodal.*_generation.provider` 字段。仅抽注册表 seam（无死后端）。36 测试通过，pyright 干净）
- [~] 点六B/C：MCP OAuth [DONE — `executor/mcp/oauth.py` 桥接：`MCPServerConfig.oauth` → 刷新型 `_OAuthManagerAuth(httpx.Auth)`，每请求向 per-server `OAuthManager` 取 token（代理 refresh 保活长连接客户端）；SSE-only，STDIO 无 HTTP auth 面。34 测试通过] / title [DONE — `session/subscribers.py:TitleSubscriber`（`ObservationSubscriber`，MIRROR/BOOKKEEPING）首个 `UserPromptSubmitEvent` fire-and-forget 一次性生成标题，`route_for_task(COMPRESSION_TASK)` 廉价模型经注入 `generate` 闭包（session 不 import router）；`_has_title` 扫日志播种 `_done` → resume 幂等；`generate_title` schema 开关（child role 关）。修复 `listing.py`：标题在首轮写于 HEAD，改 `_read_head` 用字节窗口读 title/last_prompt，tail 作 last-write-wins 覆盖。session 254 + 生命周期 10 测试通过，pyright 干净] / rate-limit [DONE — `router/ratelimit/`（snapshot/tracker/report/capture + `__init__` 门面，镜像 `router/cost/` 分层）：`RateLimitSnapshot.from_headers` 按 provider 分派解析 OpenAI（`x-ratelimit-*`，duration 如 `6m0s`）/Anthropic（`anthropic-ratelimit-*`，RFC-3339 raw）两种方言，`retry-after` 复用 `handlers._parse_retry_after`（RFC-7231 delta/HTTP-date）；`RateLimitTracker` flat `(provider,model)` 末次覆盖（非 lineage，rate-limit 是账户滚动态非累加）；`install_rate_limit_hook` 挂 SDK `._client.event_hooks["response"]`（三 provider `_rebuild_client` 各一行，闭包懒读 tracker → 存活 credential rotation），零额外请求、零 per-call 铺设；`Context.rate_limit_tracker`（`default_factory`）随 `cost_manager` 注入每个 LLM；`/usage`（别名 `/cost`）经 `backend.usage_report` 渲染 cost + rate-limit 两块。`_provider_label` 收敛为委托 `provider_label` 属性单一真源。ratelimit 23+tracker/report/capture + `/usage` 命令测试通过，router/llm 263 通过，pyright 干净]
