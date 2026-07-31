# Legacy 零兼容删除规格

## 1. 状态与目标

- 状态：已授权，待实施。
- 目标：删除仓库内所有由 Mote 自身历史版本产生的兼容读取、迁移、别名、fallback、shim、
  宽松解析和双写字段。最终代码只接受并生成当前 canonical 格式。
- 发布语义：这是有意的数据与 API 断代，版本从当前 `1.2.x` 直接进入 `2.0.0`；不再设置
  deprecation window。
- 数据语义：旧配置、旧 session、旧 journal、旧 code-map DB 和旧 import path 不保证可读取，
  不提供在线或离线迁移工具。可重建数据直接重建；不可重建历史由用户自行保留旧版本读取。

“完全不留兼容”指第一方历史兼容。跨进程 Gateway/OpenAI 旧路径由并行工作负责，不属于本文
实施 manifest；本文只记录接口边界，实施时禁止修改其文件或抢先删除其符号。

不属于本次删除的只有：

- Python/JSON/HTTP 等标准本身的跨实现兼容；
- 当前 schema 自身的显式版本字段；
- 作为当前公开 API 唯一入口的 package facade；
- 普通的类型兼容检查、网络协议错误分类和向后遍历算法。

上述排除项不能成为保留旧 Mote 名称、旧数据 shape 或旧行为的理由。

## 2. 最终硬约束

1. 每个输入边界只有一个当前 schema；缺字段、多字段、旧 discriminator、旧版本均失败。
2. writer 只写当前字段，不双写旧 alias。
3. reader 不 upcast、不猜测、不补历史默认值、不忽略未知事件或未知字段。
4. 没有 `legacy_*` registry、compatibility alias、迁移命令或旧文件发现路径。
5. Runtime 不持有“旧 facade 包新实现”的门面；调用方迁移到 canonical owner 后删除门面。
6. 可重建缓存遇到非当前 schema 时丢弃整个缓存并重建，不做行级/列级迁移。
7. 不可重建持久化数据遇到非当前 schema 时原子拒绝，绝不部分 replay。
8. 每个阶段结束必须可 import、可启动且定向测试通过；不得用 `skip`、`xfail` 或宽松解析使门禁变绿。

## 3. 源码事实与删除 manifest

### 3.1 Product 配置

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| 发现 `~/.mote/config2.yaml` | `product/config/sources.py` | 删除 `LEGACY_CONFIG_FILE_NAME` 及发现分支，只发现 `config.yaml` |
| models 缺少 `mode` 时推断 shortcut | `product/config/model/merge.py` | 删除 `_infer_legacy_mode` 和 `allow_legacy_missing_mode`，所有 layer 首次出现 models 时必须显式 mode |
| 离线补 mode 工具 | `product/config/model/migrate.py`、CLI `config migrate-models` | 删除模块、CLI 子命令及全部迁移测试 |
| unknown config 默认被 Pydantic 丢弃 | `product/config/base.py`、`runtime/config/base.py`、`contracts/config/**/base.py` | 所有配置基类统一 `extra="forbid"`；删除可选 strict 语义，正常加载始终严格 |
| `extra_fields` 历史逃生字段 | 上述所有 ConfigModel | 删除字段；需要扩展的配置必须有具名字段和 owner |
| MissingAPIKeyError 仍指向 config2 | `contracts/config/model/llm.py` | 改为当前 `config.yaml` 与当前字段路径；不保留旧提示 |

`product/config/diagnostics.py` 可以保留为只读报告器，但不能再决定 loader 是否严格；loader 永远
拒绝 unknown keys。`report --strict` 删除，因为 strict 不再是可选模式。

### 3.2 Session、消息与 Role 持久化

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| 旧 rollout identity/hash/时间/upcast helper | `runtime/session/codec.py` | 删除 `_SAFE_LEGACY_TYPE`、`migrated_event_id`、`legacy_occurred_at`、`unknown_legacy_event_type` |
| 未知 event domain 被忽略 | `decode_session_event()`、`reduce_session_envelope()` | session stream 内未知 event type 直接抛 `UnsupportedSessionEventError`，sequence 不前移 |
| event payload 丢弃 extra 字段 | `runtime/session/events.py::_dataclass_kwargs` | 当前事件逐个 strict decode；未知字段和缺少必需字段失败 |
| event payload 用 `.get(..., historical_default)` 补旧字段 | `runtime/session/events.py` | 当前 writer 字段全部必需；删除历史默认值 |
| 旧 terminal/kernel/browser state event | `runtime/session/events.py`、`runtime/projections/session.py`、`runtime/agent/session_manager.py` | 删除三类 event、reducer state 和转 RuntimeCheckpoint 的恢复桥；只接受 managed Runtime checkpoint |
| SessionMeta 缺 role/toolset identity 仍恢复 | `SessionMetaEvent.from_payload`、`SessionManager.validate_identity` | `role_class` 与 `toolset_manifest` 改为当前必需字段；缺失即拒绝整个 session |
| Message 读取 module/class discriminator | `contracts/conversation/messages.py` | 删除 `_LEGACY_INSTRUCT_CONTENT_TYPES` 和分支；只接受 `{type, version, value}` 当前格式并校验 version |
| Message 无法解析时返回 None 并跳过 | `runtime/session/events.py::_payload_to_message`、projection | 改为确定失败；删除 `MessageEvent.message is None` 和 skipped 计数路径 |
| BaseRole 读取 `__module_class_name` | `runtime/agent/base.py`、`runtime/agent/role.py` | 删除 `_LEGACY_ROLE_REGISTRY`、`legacy_role_type_ids` 和 fallback；只接受稳定 `type_id` |
| 基于模块名的通用多态序列化 | `runtime/serialization.py`、`RoleState`、`BaseEnvironment` | 先把生产调用迁移到稳定 contracts-owned `type_id`，再删除模块；不保留动态 import 或 module-class wire identity |

当前 session envelope 的 schema/version 校验继续存在，但用途仅为拒绝非当前数据；不提供 upcaster。
错误必须包含稳定 error code、期望版本和实际版本，不回显敏感 payload。

### 3.3 Model journal 与 RouteId 历史数据

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| route schema v1 裸字符串 upcast | `contracts/model/route_history.py` | 删除整个模块 |
| model plan 接受 schema 1/2 并推断 route version | `contracts/model/model_journal.py` | `ModelCallPlannedRecord` 只接受当前 schema 2、route schema 2；删除 before-validator |
| Runtime journal 调用历史 decoder | `runtime/models/failover/model_journal.py` | 直接使用 canonical `decode_route_id`；非 tagged wire string 失败 |
| v1 route fixtures/replay tests | `ztest/session/fixtures/model_routes_v1/`、`ztest/session/test_model_route_history.py` | 删除 fixture 与 upcast 测试，替换为旧格式确定拒绝测试 |

current writer 永远写 `default`、`task:*`、`semantic:*`。禁止通过调用 context 猜测裸名称属于 task
还是 semantic。

### 3.4 Run journal 与 EffectLedger 吸收兼容

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| RunJournal 读取旧 EffectRecord | `runtime/ledger/run_journal.py::StepRecord.from_dict` | 所有 StepRecord 当前字段必需；删除 tool/name/result fallback 和默认 external |
| writer 双写 `tool_name`/`result` | `StepRecord.to_json` | 只写 `name`/`payload` canonical 字段 |
| 沿用 `effects.jsonl` 文件名 | `JOURNAL_FILE_NAME` | 改为当前唯一 `run-journal.jsonl`；不探测旧文件 |
| EffectLedger/EffectRecord 旧 API 门面 | `runtime/tools/effect_ledger.py` | 将 tool pipeline、settlement、reconcile、executor 迁移到 RunJournal 的窄 tool-step API 后删除整个模块 |
| EffectLedgerConfig 历史命名 | `contracts/config/tool/models.py` 及 Product config | 重命名为 `RunJournalConfig`；不保留 alias 或旧 YAML 字段 |

这里不能只删除 parser fallback：只要 `EffectLedger` 类型仍是生产 API，就仍保留了旧架构。最终
`RunJournal` 是唯一 durable step authority，tool 侧通过 contracts-owned 窄 Protocol 注入，不反向
import Runtime concrete class。

### 3.5 Code-map 可重建数据库与 import alias

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| ADD COLUMN 和 `edges` 表迁移 | `runtime/code_map/store.py::_MIGRATIONS/_migrate_edges` | 删除迁移；数据库增加唯一 current schema marker |
| 部分旧 schema 被静默保留 | `_migrate_edges` 的 OperationalError fallback | 删除；marker 不匹配时关闭、删除该 code-map DB 并全量重建 |
| `extractor` 重导出 neutral model 类型 | `runtime/code_map/extractor.py` | 删除 re-export；所有调用改从 `runtime.code_map.model` import |

code-map 是派生缓存，因此不需要“旧格式拒绝后要求人工处理”；其 owner 可以安全删除自己的单个
DB 文件并重建，但不得删除 workspace、session 或用户源文件。

### 3.6 Workflow、Agent 与 Product API shim

| 历史兼容 | 当前位置 | 最终动作 |
| --- | --- | --- |
| 无 Output marker 时返回整个 GraphState | `orchestration/workflows/channels.py` | Workflow 编译要求至少一个显式 Output，或显式声明 `NoOutput`；删除全 state fallback |
| `env.run(k)` bounded pump | `turn_scheduler.py`、`control.py`、`environment_facade.py` | 用语义明确的测试/嵌入式端口 `run_ready_turns(max_turns=...)` 取代；删除 `run` alias |
| workflow errors 从旧 types 模块重导出 | `orchestration/workflows/types.py` | 内部与测试直接从 canonical contracts/runtime owner import；package 根只导出正式公开面 |
| environment exceptions 中转模块 | `orchestration/agents/exceptions.py` | 调用方迁移到 canonical error owner 后删除中转模块 |
| Agent prompt 的 SUBAGENT_* alias | `product/toolsets/builtin/agent_prompts.py` | 删除 alias 和验证 alias 的测试 |
| network policy 重导出 pattern helpers | `runtime/sandbox/network/policy.py`、`runtime/sandbox/network/__init__.py` | 调用方直接从 `patterns` import；policy 只导出 policy API |
| tool_result、compress 等标记为旧路径的重导出 | `runtime/tools/tool_result.py`、`runtime/tools/compress/base.py` | 逐调用方迁移后删除历史 re-export；正式 package facade 不受影响 |
| Context source 的 optional enable callback fallback | `runtime/context/turn/sources/tool_catalog.py`、`skill_listing.py` | callback 变为必需 canonical policy port；删除 `None` 表示旧行为 |
| Textual ask 将结构结果压回旧 string | `product/interfaces/textual/port.py::ask` | Human interaction 全链路使用当前结构化 answer；删除 flatten fallback |
| Browser legacy screenshot presenter | `product/interfaces/textual/surfaces/browser.py`、bootstrap | 删除 presenter 和注册；browser 使用统一 live-window/current surface presenter |
| hook legacy notify/旧行为说明 | `runtime/hook/types.py` 及 hook contracts/config | 删除已被 Stop 覆盖的 notify shape、matcher/config key 和解析分支；Stop 是唯一语义 |

`BrowserWindowPresenter` 若只是给统一 presenter 固定参数的便利子类，也仍删除：其“browser 旧专用
surface”名称本身就是第二入口。bootstrap 直接注册 canonical presenter descriptor。

### 3.7 并行工作边界：模型跨进程 Gateway 与旧 provider 数据平面

源码事实：

- `contracts/config/inference/models.py` 仍定义 `EMBEDDED | SHARED_PROCESS`，且默认是 `EMBEDDED`；
- `product/models/runtime_generation.py` 的 Embedded 分支仍在应用进程构造连接池、credential wire
  access、provider transports、receipt store、permit signer 和 `EmbeddedInferenceRuntime`；
- 同文件 Shared 分支虽通过 UDS/gRPC 调用 daemon，但应用侧仍持有 credential handles 和
  `ProductModelBindingResolver`；
- Shared `GenerationArtifact` 目前只携带 topology revision 与 credential epoch，daemon 无法据此独立
  构造 endpoint、credential binding 和 provider transport；
- `runtime/models/clients/`、`product/models/providers/`、`product/models/registry.py` 与 bootstrap 仍保留
  `BaseLLM/OpenAILLM/AnthropicLLM/OpenAIResponsesLLM` 旧客户端体系；
- `product/models/transports/` 已有显式 OpenAI Chat/Responses、Anthropic、Google、Bedrock transport，
  但当前主要由 Embedded application builder 装配；未知 transport 还会落入默认
  `OpenAIChatTransport`。

全局最终唯一数据流应为：

```text
Product config/compiler
  -> signed canonical GenerationArtifact
  -> authenticated UDS/gRPC Gateway control plane
  -> daemon-owned generation
       -> daemon-owned credential references/leases
       -> explicit transport registry
       -> provider wire

Kernel/Runtime model call
  -> contracts InferenceRuntime port
  -> authenticated UDS/gRPC Gateway execution plane
  -> lifecycle events / receipt
```

以下由 Gateway 并行工作负责，本文不实施：

1. 删除 `DeploymentMode` 分支，生产与测试 composition root 均只装配跨进程 Gateway client；测试
   provider 通过 in-process fake Gateway port 注入，不复活 Embedded production runtime。
2. 应用进程不创建 provider HTTP/WebSocket client、不读取 wire credential material、不持有 daemon
   endpoint resolver；credential material 的唯一 wire access owner 是 daemon。
3. `GenerationArtifact` 必须升级为 daemon 可独立装配的完整、签名 canonical artifact：公开 topology、
   route/endpoint execution policy、credential reference identity/epoch、transport registry revision、adapter
   revision、governance policy和 activation fencing。artifact 不含 secret material。
4. credential source 必须提供 daemon 可解析的窄引用，或由 daemon 自己拥有 source configuration。
   Python 对象 handle 不能跨进程，禁止应用先解析 material 再通过 RPC 传 secret。
5. daemon stage 必须先完成 artifact signature、schema、transport registry、credential reference、readiness
   校验，再原子 activate；失败保持旧 generation。
6. 删除 `build_embedded_model_runtime_generation()`、Embedded permit/receipt/usage 装配和应用侧
   `_build_transports/_auth_headers/_aws_credentials`。
7. `product/models/transports/` 移到 Gateway daemon 明确 owner（可保留 Product 层目录，但只能被 daemon
   composition root 可达）；架构测试禁止 application/CLI/Role composition root 直接可达 transport。
8. transport registry 不设默认项。`anthropic_messages`、`google_generate_content`、`bedrock_anthropic`、
   `openai_chat`、`openai_responses`、`openai_realtime` 等均为显式 contract ID；不接受 `openai`、品牌名、
   model 名或 unknown-as-openai fallback。
9. 删除旧 `BaseLLM` provider client 体系。Kernel Think 只看 provider-neutral inference port、canonical
   request/result；tool schema、stream event、usage、error classification 全部由 Gateway contracts 表达。
10. 删除 `CompatibilityConfig` 命名和兼容 API toggle。仍需要的 body/frame/precommit 安全上限进入明确
    的 `GatewayLimitsConfig`；不需要的 inference/admin/webhook compatibility surface 直接删除。
11. Shared daemon RPC 只支持唯一 current protocol version；删除 `(3, 2)` negotiation fallback、旧 protobuf
    reader/writer range 和 generation `migration_set_digest`。不协商旧版本，版本不等即拒绝。
12. 删除 “Shared/Embedded” 术语分叉；最终只有 `GatewayClient`、`GatewayDaemon` 和 test fake，Shared 是
    事实而非可选 deployment mode。

本文实施期间不得修改以下边界：

- `contracts/config/inference/`；
- `contracts/inference/` 与 `contracts/ports/inference/`；
- `runtime/inference/`；
- `product/inference/daemon/`；
- `product/models/runtime_generation.py`、`product/models/transports/`、`product/models/providers/`；
- `runtime/models/clients/` 及旧 BaseLLM provider 调用链。

若本文其他清理需要修改这些边界，当前阶段停止并重新切分接口，不用兼容 adapter 绕过。最终发布
签字仍需确认并行工作已完成本节列出的全局硬约束；但其代码、测试和删除动作不计入本文完成量。

## 4. 不属于运行时兼容、只需净化文字的命中

以下命中不包含旧输入分支：Squilla 对已删除 v4/single-LightGBM 的说明、logging 与旧 logs.py 的
比较、Read 对旧 offset 行为的注释、普通类型兼容检查。实施时：

- 若注释仍解释当前设计的由来且不承诺旧行为，可改为现在时；
- 若只描述已删除实现，直接删除；
- 不得为了获得 `rg legacy` 零结果而改名错误分类；Gateway/OpenAI 命中交由 3.7 节并行工作审计，
  本文不修改。

## 5. Canonical 失败语义

### 5.1 配置

- `config2.yaml` 不被发现；若只有该文件，应用表现为“当前 config.yaml 不存在”。
- 任一 models layer 缺 `mode`：`ConfigValidationError(code="MODE_REQUIRED", path="models.mode")`。
- 任一 unknown key：正常启动和 reload 均失败，错误包含 provenance 逻辑路径，不含 secret value 或
  用户 home 绝对路径。
- 删除 `migrate-models` 后 CLI 对该子命令返回标准 unknown-command，不留“已删除”分支。

### 5.2 不可重建历史

- session/model journal/run journal 在读取任何业务记录前验证 current schema identity。
- 旧/未知格式返回稳定 `UnsupportedPersistentFormatError`，不做部分 replay、不创建新 current writer、
  不原地修改旧文件。
- resume、fork、recall、listing 对同一坏 session 得到一致结论；listing 可显示“unsupported”状态，
  但不得解析旧 payload 生成预览。

### 5.3 可重建缓存

- code-map schema 不匹配时，仅删除已解析出的、位于 code-map owner 根目录内的具体 DB 文件。
- 删除失败则 fail closed，不在未知 schema 上继续查询。
- 重建无 migration event；这是 cache miss，不是历史数据升级。

## 6. 实施顺序

### 阶段 A：严格 Product 输入

1. 先让所有当前模板、fixture 和测试显式 `mode`、使用 `config.yaml`。
2. 配置基类改为 forbid extra，正常 loader 永远严格。
3. 删除 missing-mode 分支、config2 discovery、migration CLI/module。
4. 删除 prompt aliases；`runtime/serialization.py` 留到阶段 B 与持久化调用方原子迁移。

阶段结束：当前配置可启动；任何旧配置确定失败；Runtime/Kernel 不受 Product 输入类型污染。

### 阶段 B：严格 current persistence contracts

1. 收紧 Message、SessionMeta、SessionEvent payload。
2. writer 全部先切到 current 必需字段。
3. 删除 legacy event types、Role registry、route upcaster、unknown-event skip。
4. 删除旧 fixture，新增拒绝 fixture。

同一提交内完成 writer/reader/replay/projection，禁止出现 current writer 生成 reader 不接受的数据。

### 阶段 C：统一 RunJournal

1. 在 contracts 定义 tool-step journal 窄端口。
2. tool pipeline、settlement、reconcile、executor 迁移到 canonical RunJournal。
3. 收紧 StepRecord codec，切换当前文件名。
4. 删除 EffectLedger、EffectRecord、EffectLedgerConfig 及全部 alias。

### 阶段 D：删除可重建 DB 和 API shim

1. code-map 切换 current schema marker + rebuild，删除 migration/re-export。
2. workflow 输出全部显式化后删除 whole-state fallback。
3. 删除 `env.run(k)`、exception/type/re-export、context policy fallback。
4. 删除 legacy Textual/browser/hook 表面。

### 阶段 E：最终闭包

1. 删除只为兼容存在的测试、fixture、CLI help、exports 和注释。
2. 执行 import/reachability/历史格式拒绝/secret capture 门禁。
3. 与 Gateway 并行工作做边界 diff audit，确认双方没有引入临时 adapter 或覆盖彼此修改。
4. 全局清理全部合入后由发布 owner 将版本改为 `2.0.0`。

## 7. 固定测试门禁

遵守仓库约定，不用全仓无界 pytest。每阶段运行定向集合；最终大集合由资源允许的 CI 执行。

### A

```bash
python -B -m pytest \
  ztest/config \
  ztest/cli/test_model_composition_startup.py \
  ztest/architecture/test_layer_dependencies.py \
  -q --tb=no -p no:cacheprovider
```

必须新增：config2 不发现、missing mode 失败、unknown key 普通加载失败、reload 原 generation 保持。

### B

```bash
python -B -m pytest \
  ztest/session/test_codec.py \
  ztest/session/test_events.py \
  ztest/session/test_replay.py \
  ztest/session/test_role_logging.py \
  ztest/session/test_model_route_history.py \
  ztest/router/llm/test_model_call_journal.py \
  -q --tb=no -p no:cacheprovider
```

测试必须反向证明旧 fixture 被拒绝，而不是成功 upcast。拒绝后文件 SHA-256 不变。

### C

```bash
python -B -m pytest \
  ztest/executor/test_effect_ledger.py \
  ztest/executor/test_tool_executor.py \
  ztest/session/test_reconcile.py \
  ztest/runtime/test_execution_transaction.py \
  -q --tb=no -p no:cacheprovider
```

实施中测试文件随 canonical 命名迁移；最终不得再有 `test_effect_ledger.py` 名称或 EffectLedger import。

### D

```bash
python -B -m pytest \
  ztest/context/code_map \
  ztest/workflows \
  ztest/agents \
  ztest/runtime/test_browser_surface_presentation.py \
  ztest/hook \
  -q --tb=no -p no:cacheprovider
```

### E

```bash
python -B -c "import mote; import mote.product.entrypoints.cli.__main__"
python -B -m pytest \
  ztest/architecture/test_layer_dependencies.py \
  ztest/architecture/test_model_composition_reachability.py \
  ztest/architecture/test_product_dependencies.py \
  ztest/architecture/test_runtime_governance.py \
  -q --tb=no -p no:cacheprovider
git diff --check
```

Gateway 测试由并行工作运行。本文只运行既有 application startup smoke，验证非 Gateway 清理没有破坏
其公开边界，不修改 Gateway 测试预期。

## 8. 最终静态删除门禁

以下扫描在第一方生产代码中必须无命中；测试只允许“旧格式应被拒绝”的 fixture 名和断言文本：

```bash
rg -n -i \
  "legacy|back-compat|backward.compat|compat.shim|deprecated|allow_legacy|LEGACY_|upcast" \
  contracts kernel runtime orchestration product --glob '*.py'

rg -n \
  "config2\.yaml|migrate-models|__module_class_name|legacy_role_type_ids|EffectLedger|EffectRecord|effects\.jsonl|decode_historical_route_id|route_schema_version: Literal\[1, 2\]" \
  contracts kernel runtime orchestration product --glob '*.py'

rg -n \
  "SUBAGENT_TASK_PROMPT|SUBAGENT_SECTION_TEMPLATE|BrowserWindowPresenter|allow_legacy_missing_mode|unknown_legacy_event_type|migrated_event_id|legacy_occurred_at" \
  contracts kernel runtime orchestration product --glob '*.py'

```

第一条扫描对普通类型兼容术语及 3.7 节并行工作文件进行人工核验。第二、第三条是本文硬零命中。

## 9. 完成定义

只有同时满足以下条件才可宣布完成：

- manifest 中所有历史 reader、writer alias、shim、migration 与 re-export 已删除；
- 当前 writer/readers 使用唯一严格 schema；
- 旧不可重建数据确定拒绝且不被修改；
- code-map 旧缓存只触发安全重建；
- 3.7 节边界未经本文修改，并已与 Gateway 并行工作的 diff 完成冲突审计；
- Product、Runtime、Orchestration、Kernel 分层扫描通过；
- 当前配置首次启动、reload、session 创建/resume、模型 journal、tool crash reconciliation、workflow
  与 UI smoke 均通过；
- 没有新增 skip/xfail/ignore、隐藏 feature flag 或“临时”兼容入口；
- 发布版本为 `2.0.0`，release note 明确这是无迁移工具的数据/API 断代。
