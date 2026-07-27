# 统一有状态交互运行时设计

> 状态：设计草案（RFC）  
> 范围：Browser、Device、Terminal、Python Kernel、Jupyter、Canvas 及未来有状态交互工具  
> 目标：建立统一的运行时身份、生命周期、持久化、实时展示、所有权与 Handoff 基础设施  
> 非目标：以一个巨型基类统一所有领域命令，或让 Canvas/Jupyter/Browser 相互吸收

## 1. 背景

Mote 已经拥有多种有状态工具：Browser 持有浏览器上下文，Device 持有设备连接，Terminal 持有 PTY，Python 持有 Kernel。Canvas 与真正的 Jupyter Notebook 也会引入长期存活、可恢复、可被用户接管的状态。

这些工具目前或未来都会遇到同一组基础问题：

- 谁拥有当前写权限；
- 状态如何编号、并发修改如何冲突；
- 外部进程退出或 Mote 重启后如何恢复；
- 如何把实时状态呈现给 Textual、Web 或协议消费者；
- 如何把操作权临时交给用户，再安全交还给 Agent；
- 如何区分实时画面、持久产物和模型上下文；
- 如何统一关闭、清理、审计、权限和故障恢复。

若继续在每个工具内部实现 session 字典、pending restore、截图返回和私有 `askhuman/assist` 式交互，系统会形成多套相似但不兼容的生命周期。Canvas 不应成为第五套特例，而应成为统一基础设施上的领域实现。

本文提出四个公共支柱：

1. **Managed Runtime**：有状态外部或内部执行实体；
2. **Live Surface**：Runtime 的实时可观察、可交互投影；
3. **Artifact**：不可变、可持久化、可引用的产物；
4. **Handoff**：Runtime 写入权在 Agent 与 Human 之间的正式转移。

## 2. 设计原则

### 2.1 统一控制平面，不统一领域数据平面

所有有状态工具共享身份、lease、revision、checkpoint、surface、handoff 和 cleanup；但 Browser 仍操作 DOM，Terminal 仍操作 PTY，Canvas 仍操作 SceneDocument，Jupyter 仍操作 Notebook/Kernel。

公共层不得出现 `navigate`、`insert_shape`、`execute_cell` 等领域方法，也不得通过 `if kind == "browser"` 分支了解具体工具。

### 2.2 真相源、观察和展示分离

- Runtime 或其领域文档是可变状态的真相源；
- Checkpoint 是恢复点；
- SurfaceFrame 是可丢弃的实时观察；
- ArtifactRevision 是不可变产物；
- EventBus 负责低频语义事件和审计，不是 Runtime 状态库，也不承载高频帧。

### 2.3 所有写入都有身份、版本和所有者

每次写入必须关联：

- `runtime_id`；
- `runtime_revision`；
- `owner_id`；
- `fencing_token`；
- `operation_id` 或 tool call ID。

成功写入原子推进 revision；失败写入不得发布新 revision。

### 2.4 Handoff 是所有权转移，不是问答

`AskUserQuestion` 是 Agent 向用户索取值、澄清或选择的唯一问答工具；持久化领域工具不得再设计自己的 `askhuman`、`assist` 或问答 action。Handoff 用于让用户直接操作 Runtime，必须有 lease、状态机、完成/取消/超时结果和交还后的重新观察，不能依赖“请操作完后回复一句话”的问答约定。

### 2.5 恢复能力必须诚实

不同 Runtime 的恢复保真度不同。协议必须显式表达 `none | logical | partial | full`，不能把重新打开 URL、恢复 cwd 或重启 Kernel 描述成完整恢复。

### 2.6 不长期保留双轨协议

迁移期间可以按小步提交保持主干可运行，但某个领域迁移完成后应删除旧 session、media、handoff 和 restore 路径。兼容层必须有明确删除阶段，不能成为永久架构。

## 3. 核心术语

### 3.1 Runtime

Runtime 是有身份、可变、可能持有外部资源的运行实体，例如：

- 一个 Playwright BrowserContext；
- 一个 ADB/模拟器连接；
- 一个 PTY shell；
- 一个 Python Kernel；
- 一个 Jupyter Kernel 与其 Notebook 绑定；
- 一个 Canvas SceneDocument 与渲染后端。

Runtime 不等于工具实例。一个工具可以管理多个 Runtime，一个 Runtime 也可以被独立的 Handoff 工具引用。

### 3.2 Surface

Surface 是 Runtime 的实时投影，例如：

- Terminal 的字符网格或增量文本；
- Browser 的 viewport screenshot 或 accessibility tree；
- Device 的 screenshot 或 UI outline；
- Canvas 的 SVG/PNG preview；
- Jupyter 的 cell/output/widget 视图。

Surface 是最新值语义。中间帧可以合并或丢弃，最终帧和状态边界必须可靠。

### 3.3 Artifact

Artifact 是不可变、可持久化、可引用的内容，例如 PNG、SVG、PDF、`.drawio`、`.ipynb`、终端 transcript 或 Notebook 输出。

Surface 可以引用短生命周期 blob；当内容需要进入历史、导出或长期保留时，将其提升为 ArtifactRevision。

### 3.4 Handoff

Handoff 是 Runtime 写权限从 Agent owner 转移给 Human owner，再转回 Agent 的受管过程。Live Surface 的工作面与 Handoff 控制面相互独立：领域可以声明 embedded 或 window placement；当前 Browser、Device、Terminal、Jupyter 与 Canvas 均使用按需独立窗口。TUI 控制面只提供可选留言以及明确的“完成/取消”动作。它不通过普通问答消息等待用户回复，仍必须经过同一状态机和 fencing 规则。

## 4. 总体架构

```text
Domain Builtins
Browser / Device / Terminal / Canvas / Python / Jupyter
                         │
                  RuntimeHandle
                         │
              ManagedRuntimeHost
     ┌──────────────┬────┴─────┬──────────────┐
     │              │          │              │
 RuntimeRegistry  Lease     Checkpoint   HandoffCoordinator
     │           / Fence       Store            │
     │              │          │          HumanInteractionPort
     │              │          │
     └──────────────┴────┬─────┘
                         │
                   RuntimeDriver
     BrowserDriver / PtyDriver / DeviceDriver /
     CanvasDriver / KernelDriver / JupyterDriver
                         │
                SurfaceHub + ArtifactStore
                         │
        SurfacePresenterRegistry / WindowSurfacePresenter
              ManagedExternalProcess / CDP transport
                         │
             Textual / Web / Terminal / ACP / AGUI
```

依赖方向继续遵守：

```text
contracts <- kernel <- runtime <- orchestration <- product
```

- `contracts/`：值对象、事件、错误和 Protocol；
- `kernel/`：不感知具体 Runtime；
- `runtime/`：RuntimeHost、lease、checkpoint、artifact、surface、handoff 执行语义；
- `orchestration/`：只通过 RuntimeRef 编排粗粒度任务，不接管 Runtime 内部状态机；
- `product/`：Builtin 工具、领域 driver 装配、Canvas 场景语义、Jupyter 集成和 UI。

## 5. Runtime 身份与版本

### 5.1 RuntimeRef

```python
@dataclass(frozen=True)
class RuntimeRef:
    runtime_id: str
    kind: str
    alias: str = "default"
```

`runtime_id` 是稳定、opaque 的机器身份；`kind:alias` 是 session 内供模型和用户使用的可读引用，例如：

```text
browser:default
terminal:default
canvas:architecture
jupyter:analysis
```

alias 不能作为全局存储主键，也不能编码本地路径。

### 5.2 RuntimeDescriptor

```python
@dataclass(frozen=True)
class RuntimeDescriptor:
    ref: RuntimeRef
    state: RuntimeState
    epoch: int
    revision: int
    capabilities: RuntimeCapabilities
```

- `epoch`：底层进程或 driver 每次重新创建时递增；
- `revision`：语义状态每次成功修改后递增，跨 epoch 保持单调；
- `frame_seq`：Surface 内部排序号，不进入 RuntimeDescriptor；
- `fencing_token`：属于当前 LeaseEpoch，不属于 RuntimeDescriptor。

### 5.3 生命周期状态

```text
declared
  → starting
  → ready
  → busy
  → handed_off
  → restoring
  → degraded
  → closing
  → closed
  → failed
```

状态转换由 RuntimeHost 统一执行并产生低频事件。Driver 不能自行伪造生命周期状态。

## 6. RuntimeCapabilities

```python
@dataclass(frozen=True)
class RuntimeCapabilities:
    checkpoint_fidelity: Literal["none", "logical", "partial", "full"]
    handoff_modes: frozenset[str]
    surface_kinds: frozenset[str]
    multi_instance: bool = False
    optimistic_writes: bool = False
    background_survival: bool = False
```

能力由 driver 声明，RuntimeHost 执行前校验。前端也声明自己的 surface 与 handoff 能力，Coordinator 在双方能力交集上选择实现。

第一阶段只要求所有目标 Runtime 支持 `exclusive` handoff。平台缺少可用 handler 时必须在转移所有权前返回结构化 `unavailable`，不能先锁住 Runtime 再等待一个不存在的 UI。

## 7. ManagedRuntimeDriver

公共 Protocol 只描述基础生命周期：

```python
class ManagedRuntimeDriver(Protocol):
    kind: str
    capabilities: RuntimeCapabilities

    async def start(
        self,
        checkpoint: RuntimeCheckpoint | None,
    ) -> DriverStartResult: ...

    async def health(self) -> RuntimeHealth: ...

    async def checkpoint(self, reason: str) -> DriverCheckpoint: ...

    async def prepare_handoff(
        self,
        request: HandoffRequest,
    ) -> DriverHandoffHandle: ...

    async def finish_handoff(
        self,
        handle: DriverHandoffHandle,
        outcome: HumanHandoffOutcome,
    ) -> DriverHandoffResult: ...

    async def aclose(self) -> None: ...
```

领域 driver 可以扩展该 Protocol：

```python
class BrowserDriver(ManagedRuntimeDriver, Protocol):
    async def navigate(self, url: str): ...
    async def snapshot(self): ...

class CanvasDriver(ManagedRuntimeDriver, Protocol):
    async def apply(self, transaction: CanvasTransaction): ...
    async def inspect(self, query: CanvasQuery): ...
```

不得建立包含所有领域方法的 `BaseStatefulTool`。

## 8. RuntimeHost 与访问事务

RuntimeHost 是统一所有者，负责：

- Runtime 创建与注册；
- driver 生命周期；
- read/write access；
- revision 与 fencing；
- checkpoint 调度；
- Surface publisher 绑定；
- Handoff 协调；
- session cleanup 与 crash reconciliation。

领域工具通过受管访问事务调用 driver：

```python
async with runtime_host.access(
    runtime_ref,
    mode="write",
    owner_id=tool_call_id,
    expected_revision=expected_revision,
) as access:
    result = await access.driver.apply(command)
    access.commit(changed=True)
```

不变量：

1. `commit()` 至多一次；
2. 未 commit 或异常退出不增加 revision；
3. 发布新 revision 前，领域状态和必要 journal 必须持久化；
4. 过期 fencing token 永远不能提交；
5. read access 不得隐式写状态；
6. driver 的异步回调若发生外部变化，必须通过 RuntimeHost 的 `mark_dirty`/external mutation seam 对账。

## 9. Lease 与 Fencing

现有面向 run/output 的 lease 语义应上提为通用资源协调：

```python
class LeaseCoordinator(Protocol):
    def acquire(self, subject: LeaseSubject, owner_id: str, ttl: float) -> LeaseEpoch: ...
    def renew(self, lease: LeaseEpoch, ttl: float) -> LeaseEpoch: ...
    def release(self, lease: LeaseEpoch) -> None: ...
    def assert_current(self, subject: LeaseSubject, fencing_token: int) -> None: ...
    def guard(self, subject: LeaseSubject, fencing_token: int): ...
```

`LeaseSubject` 可表示：

```text
run:<run-id>
runtime:<runtime-id>
artifact:<artifact-id>
output:<output-id>
```

Runtime access 和 Handoff 必须使用同一套 fencing 机制，不能在 Canvas 或 Browser 内再实现互斥锁协议。进程内 lock 只用于性能和协程串行化，不能替代持久 fencing。

## 10. Checkpoint 与恢复

### 10.1 统一信封

```python
@dataclass(frozen=True)
class RuntimeCheckpoint:
    runtime_id: str
    kind: str
    epoch: int
    revision: int
    codec: str
    schema_version: int
    payload_ref: str
    digest: str
    sensitivity: str
    fidelity: str
```

rollout 只保存 checkpoint 引用和 Runtime 生命周期，不直接保存大型 payload、图片、Notebook 或 Canvas 场景。

### 10.2 Codec

每个 driver 注册版本化 codec：

```text
mote.terminal.logical-state@1
mote.browser.logical-state@2
mote.python-kernel.logical-state@1
mote.canvas-scene@1
mote.jupyter-notebook@1
```

要求：

- 禁止 pickle；
- schema version 显式存在；
- migration 使用固定 fixture 测试；
- digest 在恢复前校验；
- 敏感 payload 经过加密或安全存储策略；
- 恢复失败进入 `degraded`，并返回真实恢复报告。

### 10.3 恢复保真度

| Runtime | 可恢复内容 | 不保证恢复的内容 | Fidelity |
| --- | --- | --- | --- |
| Browser | URL、tab、storage/profile | DOM、JS heap、scroll、网络请求 | logical |
| Device | 设备配置、连接目标、当前 app 提示 | 第三方 app 内部瞬时状态 | partial/none |
| Terminal | cwd、env diff、shell 配置 | 任意前台进程内存、TTY 内部状态 | logical |
| Python Kernel | cwd、env、可选启动脚本 | 任意 Python heap、线程、文件句柄 | partial |
| Jupyter | Notebook 文档、cell、output | Kernel heap 的完全等价恢复 | notebook full / kernel partial |
| Canvas | SceneDocument、operation journal、metadata | 后端进程瞬时 UI 状态 | full |

Python Kernel 与 Jupyter 必须保持概念分离。当前持久 Python 执行环境不能仅通过改名变成 Jupyter。

## 11. Live Surface

### 11.1 SurfaceFrame

```python
@dataclass(frozen=True)
class SurfaceFrame:
    runtime_ref: RuntimeRef
    surface_id: str
    runtime_epoch: int
    runtime_revision: int
    frame_seq: int
    representation: str
    blob_ref: str
    final: bool = False
```

一个 Runtime 可以发布多个 Surface：

- Browser：`viewport`、`accessibility`；
- Device：`screen`、`outline`；
- Terminal：`terminal-grid`、`transcript-tail`；
- Canvas：`preview`、`selection`；
- Jupyter：`notebook`、`cell-output:<id>`。

### 11.2 SurfaceHub 语义

- 按 `(runtime_id, surface_id)` latest-value 合并；
- 每个订阅者独立背压；
- 中间帧允许丢弃；
- 首帧、状态边界帧和 `final=True` 帧必须可靠；
- 慢速图片解码不得反向阻塞 Runtime mutation；
- 不将高频帧写入 rollout；
- 订阅者离开可视区域后可以暂停昂贵 representation；
- resize 只生成新 frame，不增加 Runtime revision。

EventBus 继续承载 `runtime_started`、`runtime_revision_committed`、`handoff_started` 等低频事实，但不传输每一帧。

### 11.3 Presentation attachment 与 Handoff authority

Surface 的观察生命周期不等于 Handoff 的输入授权生命周期：

- Handoff handle 在 `active` 期间授予 Human 输入权，交还后立即撤销；
- presentation attachment 是按 `SurfaceDescriptor.ref` 标识的观察 token，可以跨越多次 Handoff；
- window presenter 只在首次 Handoff 时按需创建，不因普通工具调用自动弹窗；
- Handoff 结束时窗口切回只读 observer，并继续接收 Runtime 的 latest-value frame；
- 用户关闭窗口只 detach observer，不关闭 Runtime，也不改变领域状态 revision；
- 下一次 Handoff 若窗口仍存活则重绑定新 authority 并聚焦原窗口，若已关闭则创建新窗口；
- host/session 退出时由 `SurfacePresenterRegistry` 统一关闭仍存活的 presentation。

公共 `SurfaceObservationHub` 只负责 observer token、revision 唤醒和 detach/close，不了解 Canvas、Browser、Terminal 或 Jupyter。输入仍通过当前 Handoff handle 校验，长期 observer 不能借旧 handle 写入 Runtime。

## 12. Artifact

### 12.1 ArtifactRef

```python
@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    revision: int
    representation: str
    kind: str
    mime_type: str
    content_ref: str
    digest: str
    size: int
    retention: ArtifactRetention
    sensitivity: ArtifactSensitivity
    suggested_name: str
```

Artifact revision 不可变。逻辑 Artifact 可以有多个 revision 和 representation，例如同一 Canvas revision 的 SVG preview、PNG thumbnail 和 `.drawio` export。

### 12.2 ArtifactStore

ArtifactStore 负责：

- 内容寻址与去重；
- opaque content ref；
- MIME、digest、size；
- `ephemeral | session | project | pinned` retention；
- 敏感等级与访问控制；
- 本地路径、Web URL、IM upload 等 delivery 的隔离；
- 清理与 promotion。

跨层契约不得暴露任意绝对路径。`ArtifactResolver` 接受 opaque `ArtifactRef` 与显式
`ArtifactResolutionPolicy`，在读取前执行 sensitivity/size 限制，通过逻辑索引读取后
再次验证 digest/size；它从不暴露 CAS 物理路径。各 host 在自己的 async delivery
边界决定是否解析为本地展示资源。

Canvas 媒体以 `ArtifactRef` 作为唯一 byte source；`ToolResult`、
`PostToolUseEvent` 和 `ViewEvent` 不再复制它的 base64。命令通道在构造模型多模态消息的
最终边界并行解析 Artifact，并只在那里临时编码。尚未 Artifact 化的 Read/Browser/
Device byte-only 媒体继续使用唯一 inline source，迁移时禁止同时填入 ArtifactRef 与
base64。

## 13. Handoff

### 13.1 模型接口

Handoff 建议作为独立 builtin，而不是复制到每个领域工具：

```json
{
  "runtime": "canvas:architecture",
  "mode": "exclusive",
  "message": "请调整数据库和缓存的位置",
  "selection": ["database", "cache"]
}
```

领域工具负责创建和操作 Runtime，并在结果中返回 RuntimeRef。Handoff 工具只负责所有权转移。

### 13.2 状态机

```text
requested
  → validating
  → baseline_checkpointed
  → human_lease_acquired
  → active
  → reconciling
  → committed | cancelled | expired | failed
  → agent_lease_restored
```

每个状态转换必须可审计。进入 `active` 前必须持久化 baseline 和 handoff record；进程崩溃后可以识别 pending handoff，而不是重放用户操作。

### 13.3 协调流程

1. 解析 RuntimeRef 并检查 capability；
2. 根据 SurfaceDescriptor 协商 `embedded | window` presenter；
3. 创建 baseline checkpoint；
4. fence Agent write owner；
5. 获取 Human lease；
6. driver 准备 host-neutral Live Surface；presenter 将其嵌入宿主，或按需打开/重绑定独立窗口；
7. HumanInteractionPort 驱动完成/取消/超时，并接收可选 human message；
8. driver reconcile 外部修改；
9. 创建 after checkpoint；
10. 原子提交新 revision；
11. 释放 Human lease并恢复 Agent owner；window presentation 降为只读 observer，但不被强制关闭；
12. 返回结构化 HandoffOutcome。

### 13.4 通用结果

```python
@dataclass(frozen=True)
class HandoffOutcome:
    status: Literal["completed", "cancelled", "expired", "failed", "unavailable"]
    runtime_ref: RuntimeRef
    from_revision: int
    to_revision: int
    human_message: str
    detail: str
    summary: str
    resume_hint: str
```

通用结果不携带任意领域 `dict`。交还后模型使用领域工具重新观察：

- Browser：`snapshot`；
- Device：`snapshot`/outline；
- Terminal：screen/tail；
- Canvas：`inspect`；
- Jupyter：notebook/cell status。

### 13.5 隐私

Handoff 输入框默认用于用户给 Agent 留下交还说明，并作为 `human_message` 返回。密码、命令和 Notebook secret 等敏感输入必须由 Surface 的保密输入路径消费，默认不进入 `human_message`、Agent history 或普通日志。Driver 负责生成最小、可安全返回的交还摘要；原始敏感输入可以只进入受保护审计，或完全不记录。

Human 操作不需要模型授权，但仍处于同一 sandbox、文件系统和网络边界之内。

### 13.6 与 AskUserQuestion 的边界

所有普通人机问答统一通过独立的 `AskUserQuestion` builtin 完成：

- 验证码、账号、路径和自由文本输入；
- 单选、多选和确认；
- 需求澄清与实现决策。

`AskUserQuestion` 的问题模型应同时支持自由文本和结构化选择，替代旧的纯自由文本 `AskUser`。Browser、Device、Terminal、Kernel、Jupyter、Canvas driver 均不注入 `ask_user`/`ask_user_question`，也不在领域 action 中包装问答。模型先调用 `AskUserQuestion` 获得答案，再显式调用领域工具使用该答案。

HandoffCoordinator 使用专门的 HumanInteractionPort 接收 `completed | cancelled | expired` 和可选 `human_message`，不调用 `AskUserQuestion`。完成状态由弹窗按钮产生，输入文字不是结束 Handoff 的隐式信号。

## 14. 各领域实现

### 14.1 Browser

- Runtime：永远私有且 headless 的 BrowserContext、pages、profile/storage；`headless` 不作为运行期或 Handoff 开关；
- Surface：viewport screenshot、accessibility snapshot；
- Handoff：按需打开或聚焦独立 Browser Viewer；Viewer 输入只经当前 `LiveSurfaceSession`，交还后撤销 handler 并继续只读观察 Agent 操作，用户关闭窗口只 detach observer；
- 交还后：重新 snapshot，不能假设 DOM 与 baseline 相同；
- 恢复：URL/tab + profile/storage，明确为 logical。

短信验证码、账号等值统一由模型调用 `AskUserQuestion` 获取，再通过 Browser 的 `type`/`fill_form` 使用；Browser 不再保留 `assist` 或其他私有问答 action。真正的直接操作走统一 Handoff。

### 14.2 Device

- Runtime：设备/模拟器连接和 backend；
- Surface：screen、UI outline；
- Handoff：按需打开或聚焦独立 Device Viewer；tap、long press、swipe、文本与按键只经当前 `LiveSurfaceSession`，交还后 Viewer 继续只读观察，用户关闭窗口只 detach observer；
- 交还后：重新获取 screenshot 与 outline；
- Viewer 截图使用独立 `capture_screen()`，不得覆盖 Agent 语义操作依赖的 UI ref snapshot；
- 密码输入不得通过 Agent transcript 回放。

### 14.3 Terminal

- Runtime：PTY 和 shell/process tree；
- Surface：服务端使用成熟 VT parser 维护 canonical 字符网格、样式、cursor 与 bounded
  scrollback；首帧和丢帧/resize 后发布 full ANSI reconstruction，连续帧发布带
  `base_sequence` 的 raw VT delta，frontend 仅在基序列匹配时增量应用；
- Handoff：使用按需独立 xterm window presenter；raw input、IME/paste 与 resize 经当前 `LiveSurfaceSession`，交还后窗口只读观察，用户关闭即 detach；
- Handoff 期间 Agent stdin 被 fencing；
- 交还后返回安全 screen summary、exit/prompt 状态、cwd/env checkpoint；
- 不承诺恢复任意正在运行进程。

### 14.4 Python Kernel

- Runtime：持久 Python Kernel；
- Surface：stdout/stderr、display outputs、图表；
- Handoff：typed Notebook Surface 通过按需独立窗口接管；交还后撤销输入权但继续只读观察；
- Checkpoint：cwd/env 与显式支持的逻辑状态；
- 任意 Python heap 默认不可完整恢复。

### 14.5 Jupyter

Jupyter 是 Notebook 文档与 Kernel Runtime 的组合，不是 Python 工具别名：

- typed Notebook document 是 live Surface 的 canonical snapshot，`.ipynb` 是确定性持久 Artifact；
- Kernel 是 Runtime；
- code cell、stream、text、PNG 与 error output 是当前 Surface；`display_id` 更新由 canonical reducer 跨 cell 原位归并；
- kernel `input_request` 是 typed document state，只有当前 Handoff handle 能按 request-id 回复；交还会中断仍运行的人类 cell，Agent 执行不会隐式等待用户；
- arbitrary widget comm payload 不直接进入前端；后续 widget 能力必须先投影成 bounded、白名单 contract，不能把第三方 widget JavaScript 当作可信 UI 协议；
- Notebook UI 是按需打开且可跨 Handoff 保留的 window presenter；它复用公共 Chromium window shell，但拥有独立 media frontend；
- Notebook revision 与 Kernel runtime revision 分开；
- 用户编辑 cell 后，交还结果要求模型重新 inspect Notebook，而不是猜测 UI 操作。

### 14.6 Canvas

Canvas 在公共 Runtime 上增加专属领域模型：

- `SceneDocument`；
- stable element ID；
- `CanvasOperation` tagged union；
- `CanvasTransaction`；
- operation journal + periodic snapshot；
- backend-independent style/geometry；
- CanvasBackend adapters。

模型工具动作：

```text
open
apply
inspect
render
export
close
```

Handoff 不在 Canvas 内重复实现，统一调用 Handoff builtin。

Canvas 是普通 stateful leaf tool：

```text
stateful = True
is_graph_tool = False
```

Canvas transaction 是原子领域事务，不编译为 GraphSpec，也不启动 BgGraph。RunGraph 只负责编排多个粗粒度 Canvas 调用或素材任务。

## 15. Canvas 场景与后端

### 15.1 Canonical Scene

Mote 的 SceneDocument 是公共真相源，draw.io XML 不是公共契约。最低一等对象包括：

- document/page/layer/group；
- shape/text/path/image；
- connector/port/waypoint；
- parent-local transform 与 bounds；
- z-order；
- style token/theme token；
- metadata/semantic role；
- embedded ArtifactRef。

### 15.2 Transaction

```json
{
  "canvas_id": "architecture",
  "base_revision": 17,
  "operations": [
    {"op": "insert_shape", "id": "api", "kind": "rectangle"},
    {"op": "insert_connector", "id": "web-api", "source": "web", "target": "api"}
  ]
}
```

要求：

- operation tagged union；
- batch 原子提交；
- `base_revision` 冲突检测；
- tool call ID 作为默认 idempotency key；
- journal 先提交，再发布 Surface；
- 结果只返回新 revision、受影响 ID、compact summary 和 ArtifactRef；
- 模型上下文不保存完整 SceneDocument。

### 15.3 CanvasBackend

```python
class CanvasBackend(Protocol):
    name: str
    capabilities: CanvasBackendCapabilities
    async def open(self, scene: CanvasDocument) -> CanvasBackendSession: ...

class CanvasBackendSession(Protocol):
    @property
    def closed(self) -> bool: ...
    async def focus(self): ...
    async def set_human_editable(self, editable: bool): ...
    async def apply_delta(self, operations): ...
    async def replace_scene(self, scene: CanvasDocument): ...
    async def snapshot_scene(self) -> CanvasDocument: ...
    async def wait_closed(self): ...
    async def render(self) -> CanvasBackendRender: ...
    async def export(self, format: str) -> CanvasBackendExport: ...
    async def aclose(self): ...
```

建议实现顺序：

1. Native SVG reference backend，用于 CI、确定性测试和无 GUI 环境；
2. draw.io CDP/graph API backend，作为内部 adapter，不通过 MCP；
3. Graphviz、Mermaid、Skia、Web Canvas 等未来 adapter。

Backend 专属能力放入命名空间化、版本化 extension，不能把 draw.io style string 变成公共场景模型。

### 15.4 draw.io Desktop window backend

首个 window backend 复用 diagrams.net Desktop 的完整编辑器，不自研图形 UI。Mote 通过受管外部进程启动独立 profile，以仅监听 `127.0.0.1` 的 CDP endpoint 连接可见窗口，并只调用 draw.io 自身 graph API。实现要求：

- 本机安装 draw.io Desktop，或通过 `DRAWIO_PATH` 指向可信可执行文件；Mote 不静默下载应用；
- 启动、端口、健康、聚焦、关闭统一经过公共 external-process/CDP 基础设施；
- 打开时用 stable ID 将 Canonical Scene reconcile 到原生 graph，交还时重新 inspect graph；
- `org.diagrams.net/model@1` 与 `org.diagrams.net/cell@1` extension 保存后端专属 XML、style、连接信息，未知形状不得在 round trip 中丢失；
- CDP 或 draw.io 不可用时 Handoff 返回 `unavailable`，Canvas 不降级到 TUI modal；
- draw.io 只在 Canvas Handoff 时按需启动；交还时 snapshot canonical scene 后锁定编辑，但窗口继续镜像 Agent 的后续 revision；
- 用户关闭 draw.io 后 presentation 自动 detach，Canvas Runtime 继续存在；下次 Handoff 再按需打开；
- `.drawio`/PNG backend export 后续必须提升为 ArtifactRevision，不能把临时 profile 路径暴露给模型。

## 16. Tool Invocation Policy

有状态多 action 工具不能只依赖类级 `effect` 与 `graph_excluded`。引入按实际参数解析的调用策略：

```python
class ToolInvocationPolicy(Protocol):
    def resolve(self, tool, args) -> InvocationPolicy: ...
```

示例：

| 调用 | Effect | RunGraph | Human interaction |
| --- | --- | --- | --- |
| Canvas inspect | PURE | allowed | none |
| Canvas apply | LOCAL durable | allowed | none |
| Canvas export | filesystem mutation | allowed | approval by target |
| Browser snapshot | PURE observation | allowed | none |
| Terminal execute | EXTERNAL | allowed | permission dependent |
| Handoff | EXTERNAL interactive | denied | required |

`is_graph_tool` 继续作为工具是否驱动 BgGraph 的结构属性。Canvas 不是 graph tool；Handoff builtin 永远不能作为 RunGraph node。

## 17. 前端与能力降级

前端能力需要从简单的 `images` 扩展为：

```text
live_surfaces
surface_upsert
interactive_handoff
embedded_terminal
window_surface
artifact_download
```

降级规则：

- 支持 live surface：按 key 原位更新；
- 只支持静态媒体：忽略中间帧，显示最终 Artifact；
- 不支持媒体：显示 alt、摘要和可解析 ArtifactRef；
- 不支持 Handoff：在转移 lease 前返回 unavailable；
- `presentation=window` 但没有匹配 presenter：返回 unavailable，不改变 Runtime revision；
- Canvas 不降级为 TUI modal/字符画；draw.io Desktop 不可用时明确失败；
- TUI 对 window surface 只显示紧凑的 Handoff 控制面，工作面始终位于独立窗口。

Textual transcript 应增加 keyed surface operation，而不是为每一帧 append 新行。

## 18. 事件与持久化

### 18.1 低频 AgentEvent

建议事件：

```text
runtime_declared
runtime_started
runtime_health_changed
runtime_revision_committed
runtime_checkpointed
runtime_restored
runtime_closed
handoff_requested
handoff_started
handoff_completed
handoff_cancelled
handoff_expired
artifact_published
artifact_promoted
```

这些事件可以进入 EventBus、投影到 ViewEvent，并由 Recorder 选择性持久化。

当前恢复真相源使用单一 `runtime_checkpoint` session event。事件完整保存
`RuntimeCheckpoint` 的 identity、epoch/revision、codec/schema、payload ref、
digest、sensitivity、fidelity 与触发 reason；`ReplayResult.runtime_checkpoints`
按 `kind:alias` 做 last-write-wins。历史 `terminal_state`、`kernel_state`、
`browser_state` 事件只在 replay 时读取并迁移，不再产生新记录。

### 18.2 高频 Surface stream

SurfaceFrame 走 SurfaceHub，不进入通用 AgentEvent 流。SurfaceHub 可以向观察系统汇总低频指标，但不能为每帧写日志或 rollout。

## 19. 安全边界

- RuntimeDriver 不接收整个 Role 或 RoleState；
- RuntimeHost 通过窄 Protocol/Capability 注入；
- ArtifactRef 不能是未校验任意路径；
- renderer、draw.io、Jupyter server 等外部进程仍经过 sandbox 和权限系统；
- 本地 CDP/WebSocket 使用 loopback、随机凭据或 authenticated channel；
- Handoff Human owner 不能绕过 sandbox，只是不需要模型替用户授权；
- secret checkpoint 必须加密或使用专门 secret/profile store；
- Surface 截图与 transcript 要支持敏感内容策略；
- 过期 lease/fencing token 的写入必须硬失败，而不是 last-write-wins。

## 20. 可观测性与 SLO

最低指标：

- live runtime count；
- runtime start/restore/close latency；
- checkpoint size/latency/failure；
- revision conflict count；
- handoff duration/outcome/orphan recovery；
- surface produced/coalesced/dropped/delivered；
- artifact bytes/retention/promotion；
- leaked process/handle count。

推荐初始约束：

- Canvas/Device/Browser image surface 默认不超过 10–15 FPS；
- Terminal text surface可以更高频，但按 UI tick 合并；
- 每个 surface 订阅者最多保留一个未消费中间帧；
- final frame 必须可靠；
- session cleanup 后 live runtime 与后台 heartbeat 必须归零。

## 21. 测试策略

### 21.1 公共 conformance suite

所有 driver 必须通过统一测试：

- create/start/health/close 幂等；
- checkpoint/restore fidelity 报告；
- stale fence rejection；
- revision 单调；
- failed mutation 不提交；
- cleanup 无泄漏；
- Handoff complete/cancel/timeout；
- crash during handoff reconciliation；
- Surface final-frame guarantee。

### 21.2 领域测试

- Browser：tab/storage logical restore；
- Device：snapshot 与 reconnect；
- Terminal：PTY attach/detach、prompt、cwd/env；
- Kernel：cell/code 执行、display Artifact；
- Jupyter：Notebook revision 与 Kernel epoch 分离；
- Canvas：operation property tests、transaction rollback、journal replay、确定性 SVG；
- draw.io：canonical scene round-trip 与 unsupported extension preservation。

### 21.3 故障注入

在以下边界 kill 进程：

- mutation 持久化前；
- journal 写入后、Surface 发布前；
- Handoff baseline 后；
- Human lease active 时；
- after checkpoint 后、Agent lease 恢复前；
- Runtime close 中。

恢复结果必须满足 fencing 与 revision 不变量。

## 22. 分阶段迁移

### Phase 0：RFC 与契约冻结

- 评审本文术语和边界；
- 分拆 ADR：Managed Runtime、Surface/Artifact、Handoff、Checkpoint；
- 建立 fake driver conformance suite；
- 确定错误模型与事件命名。

### Phase 1：通用 Lease 与 RuntimeHost

- 泛化 LeaseCoordinator/Fence；
- 实现 RuntimeRegistry、RuntimeHandle、access transaction；
- 实现 async lifecycle；
- 保持现有工具外部行为不变。

### Phase 2：迁移现有有状态工具

顺序：

1. Terminal；
2. Python Kernel；
3. Browser；
4. Device。

每迁移一个领域，同时迁移 checkpoint、cleanup 与 health。全部完成后删除裸 `_tool_sessions` 管理、专属 pending restore 和同步 cleanup 特例。

### Phase 3：SurfaceHub 与 ArtifactStore

- 先迁移 Browser/Device screenshot；
- 再迁移 Read media 与 Python display output；
- Textual 增加 keyed surface upsert；
- 各 consumer 增加 capability downgrade；
- 删除旧 `ToolMedia -> MediaBlock` 静态链路。

### Phase 4：统一 Handoff

- 实现 HandoffCoordinator 与独立 builtin；
- Browser、Device 先迁移；
- Terminal 增加独立 xterm PTY 接管；
- Python Kernel 增加 console 接管；
- 删除 Browser/Device 内部 handoff action。

### Phase 5：Canvas

- SceneDocument 与 CanvasOperation；
- transaction/journal/checkpoint；
- Native SVG backend；
- 独立窗口 Surface presenter；
- draw.io backend（Desktop + CDP graph API，不经过 MCP）；
- Handoff 与 export/import。

Canvas 从第一天直接使用 RuntimeHost，不引入 Canvas 专属生命周期。

### Phase 6：Jupyter

- Notebook Artifact；
- Jupyter Kernel Runtime；
- cell/output Surface；
- JupyterLab Handoff provider；
- Notebook revision 与 Kernel revision 独立建模。

### Phase 7：可靠性与跨端

- pending handoff crash recovery（已完成单机 rollout 恢复）；
- 多前端连接；
- Web inline editor；
- Artifact retention 与 promotion；
- 长时间运行和资源泄漏测试；
- 后端 SDK 与 conformance 文档。

### 当前实现进度（2026-07-24）

已落地：

- `RuntimeHost`：身份、alias、revision、checkpoint、lease/fencing、统一 cleanup；
- `RuntimeCheckpointSink`、`RuntimeCheckpointRecorder` 与 `runtime_checkpoint`
  rollout event 已成为唯一 checkpoint 记录出口；生产 `RuntimeHost` 在装配时
  注入 session sink；
- 显式 checkpoint、成功且有变化的 WRITE commit、Handoff before/after 均通过
  Host 统一持久化；无变化 commit 不写，自动 checkpoint 不可用或 sink 失败不
  反向破坏已成功的 Runtime 操作；
- Terminal、Jupyter Kernel、Browser 与 Device 已迁移到 `RuntimeHost`；
- 裸 `RoleState.tool_session`、三套领域专属 pending restore 字段及对应 capability 已删除；恢复日志在 session 边界转换为通用 `RuntimeCheckpoint`，由 `RuntimeHost` 暂存并在首次 `ensure()` 成功后消费；
- Terminal/Kernel/Browser 的三套 recorder、Protocol、Role capability 与
  `record_*_state` schema 开关已删除；旧 event parser 仅承担历史 rollout 迁移；
- Browser 使用 durable profile 时，driver 将 storage state 写入加密 profile sink，
  通用 rollout checkpoint 只保存 URLs/active；未启用 profile 时仍可进行同 session
  的逻辑恢复；
- 独立 `Handoff` builtin、`HandoffCoordinator`、TUI 控制面、可选 `human_message`；
- host-neutral `LiveSurfaceSession`（snapshot/send/next_frame/detach）、`SurfaceDescriptor`（embedded/window）与 capability-based `SurfacePresenterRegistry`；
- event-driven `SurfaceObservationHub`、可跨 Handoff 重绑定的 presentation attachment，以及严格分离的长期观察 token/短期输入 authority；
- 通用 `ManagedExternalProcess` 和 Playwright-over-CDP 可见窗口连接设施；
- Terminal 使用 vendored、离线的 xterm.js 与 fit addon 独立窗口；浏览器 `onData` 直接进入 PTY，字符网格 resize 使用 `TIOCSWINSZ`，Agent 后续输入与输出继续镜像；
- Terminal Surface 已从 bounded raw transcript 升级为 canonical VT state：pyte 只负责
  服务端 terminal semantics，xterm.js 只负责窗口渲染；1 MiB bounded delta ring 支持
  正常增量帧，慢 observer 丢帧或 resize 后自动回到 full canonical frame；prompt
  sentinel 改为不可见 OSC，永不污染字符网格；
- Canvas `CanvasDocument`、stable element ID、原子 `CanvasOperation[]`、FULL checkpoint、SVG renderer；
- Canvas 强制独立窗口且不降级到 modal；Textual Canvas raster/widget 路径已删除；
- draw.io 仅在 Handoff 时按需打开；交还后锁为只读并继续实时镜像 Agent 操作，用户关窗只停止观察，下一次 Handoff 可重新打开；
- 完整 `CanvasBackend` SPI 与 draw.io Desktop backend：启动、聚焦、canonical reconcile、graph inspect、截图、`.drawio`/PNG export；
- draw.io 原生 model/cell 通过版本化 extension 无损保留，MCP 信封未进入 Mote；
- Browser Runtime 永远 headless；独立 Chromium Viewer 仅在 Handoff 时按需打开，交还后撤销输入 handler 但保留观察，关窗只 detach，下一次 Handoff 复用或重开；
- Device 复用同一独立 Chromium Viewer 与输入权屏障；触控、文本、系统按键均经 Device Runtime fencing，交还后保持实时只读镜像；
- 通用 `LiveWindowBackend` 与 `LiveWindowPresentationSession` 将独立窗口渲染、聚焦、输入权屏障、窗口复用、observer 重绑定及关闭监测与领域 presenter 解耦；
- Chromium viewer 已拆为共享 window shell 与按 media type 注册的 frontend；Browser/Device 共用 screenshot frontend，Jupyter 使用安全 DOM notebook frontend，领域 renderer 不再复制进程与输入权生命周期；
- Browser/Device observer 活跃期间共享按需采样时钟；无 observer 时自动停止，避免后台截图开销；
- Jupyter Runtime 发布 bounded typed Notebook document；Agent 与 Human execution 共用同一 cell journal，IOPub stream、text/plain、image/png 与 error 被结构化保留，HTML 等主动内容不进入 frontend；
- Jupyter Handoff 仅在请求时打开独立 Notebook 窗口，交还后输入被 fencing、窗口继续观察；关闭窗口只 detach observer，下一次 Handoff 可复用或重开；
- Canvas 从第一天直接使用公共 Runtime、Surface 与 Handoff，不依赖 BgGraph/RunGraph；
- 通用 `ArtifactStore` 契约与 SQLite durable index 已落地：一个逻辑 revision
  可原子发布多个 representation，revision 使用 optimistic fencing，幂等发布以
  持久请求指纹判定，retention 只能单调提升；
- Artifact bytes 复用 File Operations reservation-only `ArtifactRepository` 的
  reserve/stage/capture/complete/release、SHA-256、fsync、完整性校验和去重能力；
  SQLite 失败时最多留下可回收 live orphan，绝不提交指向 aborted blob 的索引；
  跨层只暴露经 durable index 验证的 opaque
  `ArtifactRef`，伪造 digest 不能借共享 CAS 读取文件快照；
- `ReliableArtifactPublisher` 使用 SQLite durable outbox 执行
  `stage → publish → acknowledge`；publish 后、ack 前崩溃通过稳定幂等键恢复到原
  revision，Role 每个进程首次 ready 时逐条 reconcile，单个损坏 CAS 不阻断健康项；
- Artifact outbox 还接受已物化 `ArtifactPublicationIntent`：projection handler 可
  引用 trusted CAS 中已经封存的 bytes，store 在 SQLite stage 前自行回读并校验
  SHA-256/size，然后复用同一 fingerprint、幂等 publish 与 ack 路径，无需复制 bytes；
- `ArtifactStore` 与 publisher 均已进入 Role component graph 和窄 capability；
  Canvas 的 publication-neutral async export seam 可一次原子发布多个
  representation，当前 native 路径提供确定性 SVG；`ToolMedia` 强制 typed
  `ArtifactRef` 成为唯一字节源，不再存在 inline base64 兼容协议；同一 scene content 的
  重复观察或状态回归复用同一 immutable Artifact；
- Canvas commit 后若 Artifact 发布失败会返回结构化 partial-success 错误，明确
  已提交 revision、失败阶段及安全重试方式，不谎称领域操作已回滚；
- Jupyter 已提供不启动 Kernel/GUI 的确定性 nbformat 4.4 `.ipynb` export seam；
  code/output/execution count 按标准结构输出，Mote 状态进入 namespaced metadata，
  PNG base64 在导出边界严格校验；
- Jupyter 每次成功执行会在退出 Runtime WRITE access 后导出并发布 `.ipynb`
  Artifact；空 code 使用 READ snapshot 重试发布而不新增 cell，export/publish 失败
  明确报告 committed revision 与 partial success，禁止重放已执行代码；
- `ToolResult.artifacts → PostToolUseEvent → ArtifactBlock → Transcript` 已成为通用
  非媒体 Artifact 展示链；Canvas 同时列出同 revision 的全部 representation refs，
  SVG 只保留 `ToolMedia.artifact`，模型通道经 ArtifactResolver 按需物化；
- Read 图片、PDF、PDF 渲染页与视频帧以及 Browser/Device 截图均通过共享
  `publish_media_artifact` 发布；多页/多帧并行 stage/publish，工具结果、事件和 rollout
  只携带 opaque `ArtifactRef`。模型消息边界才按 sensitivity/size policy 解析并临时编码；
- durable projection 已闭环：版本化 `RuntimeProjectionIntent` 不携带导出 bytes、
  路径或 CAS ref；带 projection 的 `RuntimeHost` WRITE commit 在写 lease 内捕获最终
  checkpoint，并等待单条 `RuntimeCommitFact` durable append；projector registry 可
  无 GUI/Kernel 地从 Canvas/Notebook checkpoint 重建导出，经 trusted CAS 和 Artifact
  outbox 发布后才写 durable ack；replay/Role readiness 会逐条恢复未确认请求；
- Canvas 已使用 opt-in 本地 operation WAL：`CanvasOperation[]` 在 `driver.apply()` 前
  连同 base checkpoint、目标 revision 和 projection intents durable prepare；成功
  commit 或确定失败后 complete/abort。若进程在 prepare 后、commit fact 前崩溃，
  `RuntimeHost` 从 base checkpoint 重建同一 runtime_id，确定性重放 batch、补写 commit
  fact 并继续 Artifact projection；Browser/Terminal 等外部副作用驱动不被强制重放；
- Handoff 已使用同一 rollout 建立 durable ownership lifecycle：在 driver 创建人类
  输入权之前写 `prepared`，surface 可写之后写 `activated`，正常交还时以单条
  `resolved` 记录最终 checkpoint 与 revision。进程在任一 pending 阶段崩溃后，
  `RuntimeHost` 会恢复同一 runtime identity、提升 epoch、撤销旧进程内 authority 并
  将所有权明确交还 Agent；如果 `handoff-after` checkpoint 已经 durable，则保留人类
  已提交的状态并推进 revision。恢复不会重放 pointer、keyboard、terminal 或 browser
  外部输入；
- Canvas export 已从 window presenter 生命周期中彻底分离：公共 `CanvasExportPort`
  按不可变 `CanvasDocument` 批量路由格式，SVG 使用 native backend，`.drawio` 使用
  确定性 canonical encoder，PNG 使用 draw.io one-shot CLI headless export。builtin
  的 `export_formats` 可显式请求 PNG/`.drawio`，SVG 始终作为 preview；同一 formats
  集合写入 durable projection intent，崩溃恢复无需打开、复用或依赖 Handoff 窗口；
- 结构化工具 schema 会把嵌套 Pydantic `$defs` 统一提升到 schema 根，批量声明无需私有 schema。

ArtifactResolver 与所有生产 `ToolMedia` 的 base64 双轨删除已经落地。Jupyter
`display_id` 与 typed/fenced `input_request` 已闭环；内建 widget 仍需先定义安全投影，
不接受 arbitrary comm 直通。Jupyter checkpoint 已恢复 cwd/env 与 Notebook document，
但仍不代表 Python heap 可完整恢复。Browser、Device、Terminal、Canvas 与 Jupyter 已
运行同一 driver conformance contract，并通过每类 25 轮 Handoff churn；corrupt
checkpoint 故障注入还会验证半启动资源被释放且同一 driver 随后可 clean retry。仍需
扩展更长时间的 soak 与更多 OS 级 kill/磁盘故障矩阵。

## 23. 明确不做

- 不建立包含所有领域方法的巨型 stateful tool 基类；
- 不把 Surface、Artifact、ToolResult 和 RuntimeState 混成一个数据结构；
- 不让 arbitrary Terminal stdout 成为 UI 控制协议；
- 不把任意绝对路径作为跨层 ArtifactRef；
- 不将 base64 高频复制进事件和 rollout；
- 不把 draw.io XML 作为 Canvas 公共模型；
- 不让 Canvas 生成 GraphSpec 或内部启动 BgGraph；
- 不把当前 Python Kernel 直接改名为 Jupyter；
- 不在领域工具中保留 `askhuman`、`assist` 或私有问答 action；
- 不用 `AskUserQuestion` 模拟 Handoff；
- 不以 last-write-wins 掩盖并发冲突；
- 不承诺无法实现的完整进程/heap 恢复；
- 不长期保留新旧协议双轨。

## 24. 验收标准

统一基础设施完成时应满足：

- 所有目标有状态工具由一个 RuntimeHost 管理；
- 不存在工具专属 pending restore 字段；
- 不存在工具自己维护的裸 live-session 全局/singleton；
- 所有 Runtime 支持统一、结构化 exclusive Handoff；
- 所有写入都受 revision、owner 和 fencing 约束；
- 所有 checkpoint 都有 codec、schema version、digest 和 fidelity；
- 所有实时展示经过 SurfaceHub；
- 所有持久媒体和导出经过 ArtifactStore；
- 高频帧不进入 rollout 或通用 EventBus；
- RuntimeHost 不包含领域 `kind` 分支；
- Canvas 是普通 stateful leaf tool，不依赖 BgGraph；
- Jupyter Notebook 与 Kernel 生命周期分离；
- 新旧实现迁移完成后旧路径被删除；
- Browser、Device、Terminal、Kernel、Canvas、Jupyter driver 共享同一 conformance suite。

## 25. 最终能力

完成后，Mote 不再只是拥有若干各自维护状态的工具，而是拥有一套统一的交互运行时平台：

> Agent 可以创建和持续操作任意 Runtime，实时向用户展示它，把写权限安全交给用户，在进程重启后按真实保真度恢复，并从新的 revision 继续工作。

Canvas 是该平台第一个需要完整 revision、实时 Surface、Artifact export 和双向 Handoff 的领域，但基础设施不以 Canvas 命名，也不受图形领域限制。
