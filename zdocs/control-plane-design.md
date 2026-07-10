# Agent 运行时控制平面（Runtime Control Plane）设计方案

> 让模型对**自身运行时**发出生命周期指令的统一框架：擦除/钉住某条结果、转后台、增删记忆、增删工具、换压缩策略……都是同一条硬脊柱上的可插拔成员。
>
> 目标：管道铺死、0 负债、面向十年。铁标准 —— **加第 N+1 个成员时，脊柱源码一个字符都不改。**
>
> 状态：设计（未实现）。本文定义脊柱、契约、成员目录、接口签名、落地路线与验收标准，供评审。

---

## 一、问题：模型对自身运行时零发声权

今天模型只能在**数据平面**发声：调工具 → 产出 Observation（tool_result 进 history）。对这条 Observation "未来怎么活"、对自身 runtime 怎么重配置，模型**影响力为 0**：

- 生命周期的唯一信号是工具作者静态声明的 `reconstructable: ClassVar[bool]`（`executor/base_tool.py:92`）。
- `ToolResult`（`executor/tool_result.py`）dataclass 无任何生命周期字段。
- 模型传的 args 在 `ToolMessage` 上被丢弃、且从不被生命周期逻辑读取。
- 6 种 typed `ControlOutcome`（`common/events/outcomes.py`）全是 hook/gate 发的，模型碰不到。模型仅有的发射口是：工具调用、纯文本、`<end>` 标记。

而未来需求会越堆越多：模型主动擦除读完就没用的结果、把耗时操作转后台、增删自己的持久记忆、按需增减工具、溢出时切到"保历史"压缩策略……**这些不能各写一套机制**，否则三年后就是一堆碎片化负债。需要一条统一、硬、可扩展的脊柱。

---

## 二、核心抽象：数据平面 vs 控制平面

```
┌──────────────────────────────────────────────────────────────┐
│  数据平面（已完备）                                            │
│    模型调工具 → Observation → tool_result 进 history           │
├──────────────────────────────────────────────────────────────┤
│  控制平面（本文）                                              │
│    模型发 ControlDirective → 干涉自身运行时生命周期            │
└──────────────────────────────────────────────────────────────┘
```

控制平面内部分**两个 Tier**，区别在指令粒度和可逆性来源：

| Tier | 对象 | 指令粒度 | 发射形态 | 可逆性来源 |
|------|------|---------|---------|-----------|
| **T1 Observation 生命周期** | 单条 tool_result | per-result 标注 | 形态 A（工具 arg / 返回附带）| rollout 账本 |
| **T2 Agent 自重配置** | session 级运行时状态 | 会话级 mutation | 形态 B（控制工具）| 各成员各自的路由 |

**关键认知**：T2 各成员的"闸门"和"可逆性"实现各不相同（记忆靠 file-history、工具靠能力表、压缩靠白名单）——但这**不是碎片化**。它们是**同一个 `Gate` / `ReversalRoute` Protocol 的不同实现**。结构上完全统一，只有 policy 可插拔。这正是"优雅"与"碎片"的分界线。

---

## 三、硬脊柱：五段管线（永不改）

每一条控制指令，不管是擦除还是换压缩策略，**必走且只走**这五段：

```
        模型
         │  ①Emission：工具调用 / 工具返回附带（唯一发射口）
         ▼
   ┌───────────┐
   │  Bridge   │  ②Directive：归一成 typed ControlDirective（tagged union，唯一表示）
   └─────┬─────┘
         ▼
   ┌───────────┐    registry.lookup(type(directive))  ← 固定：按 variant 查插件
   │ Dispatch  │
   │  (脊柱)   │───▶ ③Gate.admit(directive, view)      ← 可插拔：裁决（deny→短路）
   │           │───▶ ④Handler.apply(directive, view)   ← 可插拔：副作用，产出 ReversalToken
   │           │───▶ ⑤登记 ReversalToken 进恢复表      ← 强制：无恢复路由不许上线
   └───────────┘
```

- **段 ①②** 是脊柱骨头：发射口 + typed directive，**永不动**。
- **段 ③④⑤** 是三个 Protocol 插槽：`Gate` / `Handler` / `ReversalRoute`。
- **"定制新策略" = 实现这三个 Protocol + 在 registry 挂一行，脊柱零改动。**

Dispatch 循环（脊柱，伪码）：

```python
async def dispatch(self, directive: ControlDirective) -> Disposition:
    plugin = self._registry.lookup(type(directive))     # 固定：variant → 插件束
    admission = plugin.gate.admit(directive, self._view)  # 可插拔裁决
    if not admission.allowed:
        return Rejected(reason=admission.reason)
    token = await plugin.handler.apply(directive, self._view)  # 可插拔副作用
    self._reversal.register(directive, token)            # 强制登记恢复路由
    return Accepted(token=token)
```

这段循环**不知道也不关心** directive 是擦除还是换策略——它只认 Protocol。这就是 0 负债的机械保证。

> **直接对齐既有先例**：`common/events/outcomes.py` 文首已写明"加一个控制事件 = 加一个 outcome type + 一个声明 handles/stage 的 subscriber，bus 循环和现有 subscriber 全不动"。本脊柱是同一范式在"模型发声"方向的延伸——buckets 按事件名 keyed、无全局列表可乱序、无共享 struct 要拓宽。

---

## 四、三条不变量契约（评审红线）

1. **Intent, not imperative（意图而非命令）**
   模型发的是**建议**，runtime 保留裁决权：token 压力未到就不擦；能力天花板不够就拒；策略不在白名单就驳回。模型**不能强制** runtime 做不可逆的事。

2. **Every mutation reversible — via its own registered route（每个 mutation 都可逆，各走各自登记的路由）**
   上线一个成员前，**必须先答清它的恢复路由**并登记进恢复表；没有恢复路由的成员不许进。可逆性来源按成员而异（账本 / file-history / 能力表回滚 / profile 回选），但"必须有"是硬性的。
   - 地基事实：`MessageAppendedEvent` 在 fold 原地 mutate **之前**就带完整 body 落进 `rollout.jsonl`（`session/subscribers.py`），fold 的 mutation **不回写**。所以从 context projection 擦除 ≠ 销毁——history 是账本的投影，擦除只动投影，账本永留原件。

3. **Bounded authority（有界授权）——T2 专有第三条**
   模型的自重配置**只能在预设边界内浮动**：能力天花板（`RoleCapabilities`）/ 策略白名单 / 参数区间。永远不能扩展到"任意"。这条防的是 T1 没有的两个新风险：**能力逃逸**（子 Agent 给自己开危险工具）和**自毁**（模型写个坏压缩策略抹掉历史）。

统一的是这三条契约 + 五段脊柱 + 三个 Protocol；不统一的、也**只允许不统一的**是 Gate/Handler/ReversalRoute 的具体实现。

---

## 五、成员目录

### T1：Observation 生命周期（账本兜底）

| 成员 | 语义 | Gate 实现 | Reversal 实现 | 现状 |
|------|------|----------|--------------|------|
| **erasable** | 这条结果可提前从 projection 清掉 | `TokenPressureGate` | reconstructable→`Rerun`；否则→`LedgerRehydrate` | 静态 `reconstructable` 已存在，模型标注版缺 |
| **pin** | 压缩时别动这条 | `CapabilityGate` | 无副作用（不擦即可逆）| `RESOURCE_STICKY`（`common/resource/`）系统标记版已存在，模型直发版缺 |
| **background** | 这个操作挪到后台跑 | `PoolQuotaGate` | `BackgroundTaskContextSource` 回灌进度 | **已完备**（`executor/tasks/`，作参照系）|
| **checkpoint** | 标记可回退锚点 | 恒 allow | 账本已 append-only，天然支持 | 账本具备、无模型接口 |

### T2：Agent 自重配置（各自路由）

| 成员 | 语义 | Gate 实现 | Reversal 实现 | 现状 |
|------|------|----------|--------------|------|
| **memory-持久** | 增删改 MEMORY.md / 文件记忆 | `MemoryWriteGate` | **`file-history` blob**（`session/snapshot.py`）`restore()` | 模型已能裸 Edit，缺语义封装 |
| **memory-工作** | 召回 / 擦除当前 history 消息 | = T1（erasable/pin）| 账本 + **demand-paging** | 缺 demand-paging（见 §八）|
| **toolset** | 增 / 减工具 | `CapabilityCeilingGate` | `ReAddTool`（再加回来）| 增=skills 已有；减=缺；能力天花板已有 |
| **compression** | 换压缩策略 profile | `WhitelistGate`（profile 白名单）| `RestoreProfile`（选回原 profile）| **全缺**，最危险 |

三个 T2 成员各自的要点：

- **memory-持久**：不是新机制。模型今天已能 Write/Edit 写 MEMORY.md，且 file-history 已给可逆（改盘前存 before-image blob，`restore()` 回滚）。缺的只是**语义封装**——给一个 `remember/forget` 语义指令，让"写记忆"这个意图显式化、可被 Gate 裁决，而非裸编辑一个碰巧叫 MEMORY.md 的文件。
- **memory-工作** = T1 的 erasable/pin + **主动召回**（把账本里某条捞回来）。召回卡在 demand-paging（§八）。
- **toolset**：**增已 solved**——`context/skills/`（SkillTool + skill_injector）就是模型驱动的动态工具加载，`ToolCatalogContextSource` 已做 per-turn "New tools available" delta 注入 + PostCompact 全量重发。缺的是**减工具接口**（低风险，可逆=再加回来）。核心闸门**不是账本、是能力天花板**：模型只能在 `RoleCapabilities` 已授予的天花板内增减，防能力逃逸。
- **compression**：**T2 最危险**——坏策略能抹掉整段 history，而压缩本身就是可逆性边界（drop 是 destructive，账本虽在但 context 已 RESET）。安全设计的铁律：**绝不让模型自由组合 reducer 或写任意策略**（那是给模型能自毁的枪），只暴露一组**命名 profile 白名单**（如 `preserve-history` / `balanced` / `aggressive`），或更保守地只让模型拨**方向性旋钮**（"多保历史"↔"更省 token"），运行时翻译成 `ContextManager` 的参数区间。这与用户在 recovery 路径定下的"溢出时尽量保住历史"是同一诉求的显式化。

---

## 六、接口契约

全部落在**叶子层** `common/interface/`（只 import typing，谁都能 import 不成环，对齐 `protocol-arch.md` 的 10 个既有 Protocol）。

### 6.1 ControlDirective —— tagged union（段 ② 的唯一表示）

对齐 `outcomes.py` 的"一问一 type"哲学：每个成员一个小 DTO，字段只对该成员有意义，type 让契约不可伪造。

```python
# common/interface/control_directive.py  (leaf: dataclasses + typing only)

@dataclass(frozen=True)
class RetainDirective:            # T1: erasable / pin
    target_call_id: str
    mode: Literal["erasable", "pin", "default"]

@dataclass(frozen=True)
class CheckpointDirective:        # T1: checkpoint
    label: str

@dataclass(frozen=True)
class MemoryDirective:            # T2: memory-持久
    op: Literal["remember", "forget"]
    key: str
    body: str = ""

@dataclass(frozen=True)
class ToolsetDirective:           # T2: toolset
    op: Literal["enable", "disable"]
    tool_name: str

@dataclass(frozen=True)
class CompressionDirective:       # T2: compression
    profile: str                  # 必须在白名单内，否则 Gate 驳回

# background 复用既有 BgTaskResult（executor/tasks/types.py），
# 概念上是本平面成员，实现上保留原返回口，不强行归一（见 §七）。

ControlDirective = Union[
    RetainDirective, CheckpointDirective,
    MemoryDirective, ToolsetDirective, CompressionDirective,
]
```

### 6.2 三个插槽 Protocol（段 ③④⑤）

```python
# common/interface/control_plane.py  (leaf)

class RuntimeView(Protocol):
    """脊柱递给插件的只读运行时视图（token 压力 / 能力集 / profile 白名单 / 当前 history）。
    插件只读不写；写只能经 Handler.apply 的返回效果，保证唯一副作用口。"""
    ...

@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str = ""

class Gate(Protocol):                                  # ③ 裁决（可插拔）
    def admit(self, directive, view: RuntimeView) -> Admission: ...

class DirectiveHandler(Protocol):                      # ④ 副作用（可插拔）
    async def apply(self, directive, view: RuntimeView) -> "ReversalToken": ...

class ReversalToken(Protocol):                         # 不透明恢复句柄
    """Handler.apply 产出，登记进恢复表；成员各自定义其内部（blob hash / profile 名 / 账本 offset）。"""
    ...

class ReversalRoute(Protocol):                         # ⑤ 恢复（可插拔）
    async def revert(self, token: ReversalToken, view: RuntimeView) -> bool: ...
```

### 6.3 注册表（脊柱认插件的唯一入口）

```python
# 每个成员 = 一个插件束，挂进注册表一行：
registry.register(RetainDirective,      Plugin(TokenPressureGate(),   RetainHandler(),   RetainReversal()))
registry.register(MemoryDirective,      Plugin(MemoryWriteGate(),     MemoryHandler(),   FileHistoryReversal()))
registry.register(ToolsetDirective,     Plugin(CapabilityCeilingGate(), ToolsetHandler(), ReAddReversal()))
registry.register(CompressionDirective, Plugin(WhitelistGate(WHITELIST), CompressionHandler(), RestoreProfileReversal()))
# ... 三年后加新成员 = 再 register 一行，dispatch 循环不动。
```

---

## 七、model→directive 桥（段 ①→②，唯一真正的新缝）

模型只有工具调用/文本/`<end>` 三个发射口。控制指令**不能靠模型吐控制文本**（脆、要解析、native/xml 分叉）。正确的缝 = **复用工具调用作为发射口**，对齐后台任务的既有先例：

> 后台任务不是靠"模型说把它放后台"，而是工具**返回** `BgTaskResult.background()`。发射口 = 工具的返回类型。

因此桥有两种形态：

- **形态 A（工具自带）**：工具在其返回值里附带 `ControlDirective`（对齐 `BgTaskResult`），或在 schema 暴露一个可选 arg（如 `retention: "erasable"|"pin"`），executor 在拿到 `ToolResult` 后抽出 directive 喂 dispatch。**这是 T1 的主形态**——擦除/钉住是对"刚产出的这条结果"的标注，天然附在返回上。
- **形态 B（独立控制工具）**：对"回头改主意 / session 级重配置"的操作，做成显式控制工具（`RememberTool` / `EnableToolTool` / `SetCompressionProfileTool`），工具 `_run` 直接产出 `ControlDirective`。对齐 `ResumeTasks`/`CancelTasks`/`GetNodeState` 三个既有后台控制工具的先例。**这是 T2 的主形态**。

桥的实现落点：`executor/tool_executor.py`（拿到 ToolResult 后），把附带/产出的 directive 交给 dispatch。**directive 经既有 `EventBus` 的 control 平面流转**（`common/events/`），不另造消息总线——这是"复用既有平面"的关键落点。

> 现状 gap 精确定位：既有 `ControlOutcome` 是 hook/gate 发的，模型发不了。本平面唯一真正新增的东西 = **这座 model→directive 桥**。除它之外，段 ④⑤ 全是往固定插槽塞既有能力。

---

## 八、载重前提：demand-paging（唯一的重活）

**erasable 覆盖 stateful/non-reconstructable 结果、以及 memory-工作的"召回"，前提都是 mid-session demand-paging —— 从账本按需回填单条消息 —— 而这个能力今天不存在。**

现状（`session/replay.py`）：replay 只有**整会话批量重建**（`CompactedEvent` 会 RESET 整段 history），没有"给我把第 N 条 tool_result 从账本捞回来重新贴进 context"的选择性拉取。post-compact 的 rehydration（`context/compaction/rehydrate.py` FileRehydrator / `common/resource/registry.py` sticky `project()`）是**预取全量**，不是按需单点。

影响分级：

- **erasable 对 reconstructable 工具**（Read/Grep）：**今天就能安全上线**，恢复路由 = `Rerun`，已验证。**先吃这波红利。**
- **erasable 对 stateful 工具**（Terminal/Python/WebBrowser）：**必须先建 demand-paging**，否则擦了就真没了（重跑会改变状态、账本又拉不回单条）——违反不变量 2。

demand-paging 接口建议（对齐 `session/history.py` 的正向单扫风格）：`SessionLog` 加读侧 API —— 按 `tool_call_id` 从账本取单条 `MessageAppendedEvent` 的 body。这是解锁 stateful erasable + memory-工作召回的**同一把钥匙**。

---

## 九、复用既有基础设施（0 负债证明）

本平面**不造平行轮子**。新代码集中在"脊柱骨头 + 桥 + 注册表 + 各成员的 Gate/Handler/Reversal"，其余全是往固定插槽塞既有能力：

| 需要的能力 | 复用的既有件 | 位置 |
|-----------|-------------|------|
| typed 指令 + fold/短路范式 | `ControlOutcome` 家族 + EventBus control 平面 | `common/events/` |
| 发射口（工具返回附带指令）| `BgTaskResult` 返回口先例 | `executor/tasks/types.py` |
| 控制工具先例 | ResumeTasks/CancelTasks/GetNodeState | `executor/tools/` |
| T1 可逆 | rollout 账本（pre-fold body 已落盘）| `session/` |
| memory-持久 可逆 | file-history blob + `restore()` | `session/snapshot.py` |
| toolset 增 + 展示 | skills 动态加载 + ToolCatalog delta | `context/skills/`、`context/turn_context/sources/tool_catalog.py` |
| toolset 闸门 | `RoleCapabilities` 天花板 | `roles/capabilities.py` |
| pin 前身 | `RESOURCE_STICKY` | `common/resource/`、`common/const/message.py` |
| Protocol 封层惯例 | 既有 10 个 interface Protocol | `common/interface/` |

**唯一真正新增**：(1) model→directive 桥；(2) demand-paging 读侧 API；(3) compression profile 注册表。其余是组装。

---

## 十、落地路线 + 验收标准

增量、每步自洽，先低风险吃红利，重活垫后：

1. **脊柱骨架**：`ControlDirective` 初始 variant 集 + `Gate/Handler/ReversalRoute` Protocol + registry + dispatch 循环（`common/interface/` + `common/events/` 落地）。
2. **T1 erasable（reconstructable）**：`ToolResult` 加 retention 字段；FoldReducer 判据从 `reconstructable` 推广到 `reconstructable OR retention==ERASABLE`；reconstructable 工具 schema 暴露 `retention` arg。恢复=Rerun，零风险。
3. **memory-持久语义工具**：`RememberTool`/`ForgetTool` → `MemoryDirective`；Reversal 复用 file-history。低风险，可早做。
4. **toolset 减工具 + 坐实 `CapabilityCeilingGate`**：中风险。
5. **demand-paging**：`SessionLog` 按 tool_call_id 单条回填 —— 解锁 memory-工作召回 + stateful erasable。
6. **compression profile 注册表 + 模型选择工具**：最危险、最后做，且只做白名单选择。

### 验收标准（写死进本文，评审据此把关）

> **加成员测试**：假设未来要加"动态调 temperature""委派给专家 Agent""切换 reasoning 模式"等今天没想过的成员。加它 = 新增一个 `ControlDirective` variant + 一个 `Gate` + 一个 `Handler` + 一条 `ReversalRoute` + registry 挂一行。**dispatch 循环源码一个字符都不改。**
>
> 若某个新成员逼着改 dispatch 的 if/else，或逼着往某个 struct 加字段 —— **设计回炉**。这是"0 负债、面向十年"的唯一机械检验。

---

## 十一、分层落位

```
┌───────────────────────────────────────────────────────────────┐
│ 消费方（发射 / 触发）                                          │
│   工具（返回附带 directive）  控制工具（_run 产出 directive）   │
│   ToolExecutor（桥：抽出 directive → dispatch）                │
├───────────────────────────────────────────────────────────────┤
│ common/interface  ★叶子层★                                     │
│   ControlDirective(union)  Gate / DirectiveHandler /           │
│   ReversalToken / ReversalRoute / RuntimeView                  │
│ common/events                                                  │
│   ControlPlaneDispatch（脊柱）+ registry  ← 长在既有 EventBus 上 │
├───────────────────────────────────────────────────────────────┤
│ 实现方（各成员插件，散落各高层，装配点注入）                   │
│   Retain/Memory/Toolset/Compression 的 Gate+Handler+Reversal   │
│   复用 session(账本/blob) / resource(sticky) /                 │
│   capabilities / skills / context.manager(压缩)                │
└───────────────────────────────────────────────────────────────┘
```

脊柱在 `common/`（低层、零环）；成员实现散落高层，运行时由 Role/装配点按注册表注入 —— 完全对齐 `protocol-arch.md` 的"消费方在上、契约在中、实现方在下"分层律。

---

## 一句话总结

**把"模型对自身运行时的建议"提升为一等公民**：一条永不改的五段脊柱（Emission→Directive→Gate→Effect→Reversal），长在既有 EventBus/ControlOutcome 平面上；每个新能力 = 往 Gate/Handler/Reversal 三个插槽定制一个策略并注册。统一的是三条契约（意图-非命令、恢复路由强制、有界授权）；只允许不统一的是三个 Protocol 的实现。今天能立刻落地的是 reconstructable 结果的模型主动擦除 + memory 语义工具；真正的三块新地基是 **model→directive 桥**、**demand-paging**、**compression profile 注册表**。验收铁标准：**加成员不碰脊柱**。
