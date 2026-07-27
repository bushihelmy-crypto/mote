# Mote File Operations 长期架构与迁移计划

> 状态：已实施（单机生产边界）
>
> 设计周期：面向未来十年演进
>
> 范围：Read、Search、Edit、Write、snapshot、hunk review、history restore、`/rewind`
>
> 核心决策：抽取一个统一的 File Operations bounded context。产品工具只做参数适配和结果展示；所有受管文件读取、搜索、修改、恢复和状态查询共享同一套版本、编码、锁、事务与恢复语义。

> 实施状态（2026-07-24）：统一 File Operations 主路径已经落地。正式事务模型为 canonical `MutationSet(transaction_id, session_id, source, mutations, recovery_policy)`；CREATE/REPLACE/DELETE 都引用 sealed B0/B1 content artifact，PREPARED event、replicated recovery fence、完整排序锁集合和 metadata-safe atomic publisher 构成唯一发布协议。恢复按 B0/B1/OTHER vector 裁决，全 B0 abort、全 B1 commit、混合状态逆序补偿，OTHER 永远进入 IN_DOUBT。Read capture、Edit、history restore、hunk reject/undo、checkpoint、`/rewind` 与 fork 均已迁入该协议；fork 只复制 committed typed facts 和其 artifact closure，不再读取旧 artifact backend。
>
> Search 与 EditPlanner 共用唯一 `CandidateDiscoveryService` 和 immutable `RegexProgram`；regex flags、occurrence、`Match.expand` replacement semantics、候选顺序与 skipped 语义只有一份实现。项目级 `EditPlanner` 在 plan 阶段冻结候选、sealed sources、encoding、raw-byte mapping、replacement expansion、preview、transaction id 和 canonical MutationSet；`EditPlanStore.publish_edit_plan()` 在 journal exclusive lock 内完成 check-and-append，commit 不重新 discover、materialize、compile 或 expand。GBK、Shift-JIS、UTF-16 BOM、mixed newline 和未修改 byte ranges 都有保真测试。
>
> Product Edit 已一次性切到 `plan_file_edit`/`commit_edit_plan`；产品层只做参数适配与结果展示。`FileMutatingTool`、单文件 convenience writer、unreserved artifact writer 和旧四个写 capability 已删除，唯一正式发布入口是 `MutationCoordinator.commit(MutationSet, ownership)`。`ScopedMutationArtifacts | DurableEditPlanArtifacts` 是封闭 ownership union：scope 产物至少保持到 PREPARED fsync，durable plan 则必须证明 journal-rooted closure。History restore、hunk transition 和跨文件 batch reject 都通过 `MutationFactory` 建立同一种 ownership。
>
> Durable cursor/timeline 已一次性切换完成：Read 与 Search 共用 exact-schema SQLite `DurableCursorRegistry`，HMAC grant 不暴露 artifact/position，lease 具有 idle/hard TTL、canonical pin closure、确定性 advance 和跨进程 epoch fence；旧 cursor codec 已删除，不做双读。observed snapshots 也持久化在同一 registry、同一 timeline epoch 中，完整 Read 在返回前即建立 durable observation。GC 使用 registry 的 `BEGIN IMMEDIATE` frozen pin snapshot，把根集合冻结到 catalog transition 完成；不存在“读两次 revision 后仍留下提交窗口”的伪保护。
>
> Artifact lifecycle 已完成一次性收口：`ArtifactRepository` 是唯一生产 artifact store，旧 `SnapshotArtifactStore` 和所有 convenience API 已删除。所有 producer 必须先建立 exact reservation，再用一个 `ArtifactWriteScope` 覆盖完整 artifact closure；所有 consumer 只从 catalog 中的 exact LIVE ref 经单一 stable handle 验证读取。`ArtifactReachabilityProjector` 只解释 typed transaction/edit-plan/hunk events 与 exact bounded Read/Search/EditPlan manifests，未知事件、损坏对象或非法序列一律 fail closed，不扫描私有 SQLite 表，也不猜任意 JSON digest。
>
> 两阶段 GC 已正式接入 `FileOperations.collect_artifacts()`：`catalog generation snapshot → typed journal reachability → frozen cursor/observed pins → generation-fenced QUARANTINED → 独立二次扫描 → restore 或 DELETING → repository reclaim`。同 digest publish/reclaim 持跨进程 exclusive artifact lock，verified reader 从 LIVE 校验到 EOF/close 持 shared lock；物理回收固定为 `unlink → shard parent fsync → strict catalog completion`。重启会枚举并继续既有 DELETING，stale worker 遇到重新发布的同 digest 只会返回 SUPERSEDED，不能发生 ABA 误删。health 持久暴露 catalog generation、quota、reservation/stage、quarantined/deleting backlog，以及最后一次 GC 的成功/失败、完成时间和回收计数。
>
> 旧 session 输入已严格收口在一次性 schema gateway：迁移按有界 record 流式扫描，逐字节验证 legacy before-image，生成 canonical SHA-256 artifact 与 `FileHistoryImportedEvent`，不虚构旧事件不存在的 B1、identity 或 metadata；替换前建立并验证只读原始字节备份，完整新日志与引用 artifact 验证通过后才执行 `temp fsync → os.replace → parent fsync`。当前 runtime 只认识 schema v2，fork/history/GC 不依赖 legacy store；`FileSnapshotEvent`、`BlobStore`、`GitBlobStore` 和旧 snapshot runtime 已删除。
>
> Watcher 已改为 File Operations 的窄 `FileChangePort`：扫描得到 exact `FileVersion`，批量用 durable committed transaction facts 判定同一 session 的 managed transition，外部变化在 SQLite 单事务内推进 timeline epoch、使旧 cursor 失效并只移除变化路径的 observation。ChangedFiles/CodeMap 只消费 typed `FileChangedEvent` 与 durable observation，不再以 mtime 复查磁盘。Artifact GC 由 Role 明确拥有的 maintenance service 自动运行：启动立即扫描、配额压力下在第二次独立 root/pin 扫描后跳过正常 24 小时观察期并立即回收，饱和批次有界排空；所有阻塞 IO 进入工作线程，shutdown 等待当前有界周期并完整 join。
>
> 部署承诺明确限定为单机：POSIX `flock` / Windows `LockFileEx` 覆盖同一主机的线程与进程，进程崩溃由内核释放 handle。当前没有多机需求，因此不保留分布式 lease、fencing token、兼容 backend 或 feature flag。

## 1. 结论与设计立场

Mote 已经拥有比 FastCtx 更完整的写后恢复能力，因此不应再补一套 rollback。真正需要做的是把现有能力迁入统一的文件操作内核，使写前安全、写中崩溃恢复和写后撤销使用同一组事实。

本计划不采用以下妥协：

- snapshot 失败后仍继续受管写入；
- 只比较 mtime；
- 每个工具各自打开、解码和写文件；
- `open(..., "w")` 截断写；
- 普通文本和富文档使用不同的 count/regex 语义；
- hunk ledger、snapshot event、transaction journal 各自成为状态真相源；
- unbound 工具绕过安全检查；
- 以 compatibility wrapper 长期保留旧 mtime API；
- 把 advisory lock 描述成强制 OS CAS；
- 把通用文件系统上无法实现的多路径瞬时原子可见性包装成“项目事务”。

“零负债”在本文中的可验收含义是：只有一个正式抽象、一个状态真相源、一个写入入口、明确的物理保证边界；迁移完成后删除旧实现和旁路，不保留两套语义。它不意味着对普通文件系统作不可能兑现的承诺。

## 2. 源码验证结论

### 2.1 Mote 已有能力

Read 已支持：

- PDF、Word、Excel 文本提取；
- PDF 原件作为模型媒体；
- 图片缩放/原图、Notebook 展平、视频抽帧和转录；
- `offset + limit`；
- 大结果自动落盘；
- 上下文可见性去重；
- Search 结果到 Read 的文件与行号衔接。

Search 已支持：

- glob 与内容 regex 组合；
- ripgrep、`.gitignore`、hidden、上下文行；
- PDF、Word、Excel 提取文本；
- matched files fan-out；
- CodeMap glimpse。

恢复链路已支持：

- `runtime/session/snapshot.py` 的 raw before-image；
- 内容寻址 blob / 独立 Git object DB；
- rollout-backed review projection 和 `hunk_ops.py` 的 hunk accept/reject/undo；
- drift 检测；
- `runtime/session/history.py` 的文件历史恢复；
- `runtime/session/checkpoint.py` 和 CLI `/rewind` 的 turn checkpoint；
- `runtime/disk/disk_io.py::atomic_write()` 的临时文件、文件 fsync、`os.replace`、父目录 fsync。

### 2.2 初始审计确认的迁移缺口

以下是启动本次迁移时确认的基线；已完成项以文首实施状态和对应阶段测试为准，保留本表用于解释架构决策来源。

- Read/Edit 普通文本固定 UTF-8，传统编码、BOM 和未修改字节无法保真。
- read-state 只保存 `mtime_ns`，不能证明 Edit 校验的是 Read 实际看到的 bytes。
- Edit 的 mtime 检查、读取、snapshot 和截断写不在一个临界区，存在 TOCTOU。
- 普通 Edit 没有跨 Mote 进程的文件身份锁。
- `atomic_write()` 临时文件名仅含 PID，同进程并发可能冲突，且普通 Edit 尚未统一使用它。
- snapshot 是 best-effort，失败只写日志；这与“每次受管写入均可恢复”的产品承诺不一致。
- hunk status 来自进程内 folded cache；不同进程可能同时观察到 pending。
- history restore、hunk reject、`/rewind` 与 Edit 没有共享发布协议。
- Search 普通文件 count 使用 `rg -c` 统计匹配行，富文档使用 `findall` 统计 occurrence。
- Search 通过冒号拆分 `path:line:text`，对 Windows 盘符和含冒号路径不安全。
- Search 以 replacement decode 消费 ripgrep 输出，非 UTF-8 内容和路径不可逆。
- 普通文本与富文档使用不同 regex engine、ignore 和 skipped 语义。

### 2.3 FastCtx 已确认的机制

- `src/edit/mod.rs` 用文件身份维护进程内锁。
- `src/edit/locks.rs` 用 `fs2` 文件锁实现跨进程 advisory lock；lock key 是身份材料的 SHA-256。
- existing identity 在 Unix 为 device + inode，在 Windows 为 volume serial + file index。
- missing/name identity 为 canonical parent identity + normalized filename。
- 既有文件提交同时获取 name identity 和 target identity 锁，覆盖目录 entry 和路径别名两类竞争。
- `src/edit/document.rs` 冻结 B0，提交前复核 identity 和完整 bytes，保留 symlink 并拒绝 hardlink。
- `src/control/transaction.rs::atomic_replace` 使用同目录唯一临时文件、保留 Unix mode、同步后替换。
- 项目级 replace 冻结候选和总匹配数后逐文件提交；后续冲突返回 `Partial`，并不是跨项目 all-or-nothing。

Mote 应吸收双身份锁、frozen B0 和最终复核，但不照搬 FastCtx 的状态模型。Mote 的 snapshot、hunk、history 和 rewind 应成为统一事务事实的投影。

## 3. 硬性不变量

以下不变量是架构测试对象，不是编码约定：

1. 任何受管文件操作只能由 `ProjectOperationControl` 完成锁准入和恢复裁决；publisher、mutation、rewind、checkpoint、Read/Search 都不得自行读写 fence 或建立第二套 gate。
2. 成功发布前，B0、B1、事务 PREPARED 事件必须已经 durable。
3. snapshot 失败即写入失败；不存在 warning 后继续写的受管路径。
4. Read 记录的版本必须来自 Read 实际消费的 sealed bytes。
5. 发布前必须同时复核 name identity、target identity 和完整 B0 bytes。
6. 所有跨进程锁遵循同一层级顺序；没有 `/rewind` 特例。
7. 所有文件改写只能看到完整 B0 或完整 B1，不能看到截断或半写。
8. snapshot、history、hunk 和 crash recovery 只能从 durable transaction events + blob store 推导。
9. hunk 状态迁移必须是 durable expected-version transition，不能信任进程内 cache。
10. Read/Search/Edit 共享同一 encoding decision 和 regex semantics。
11. 传统编码编辑后，未修改 byte ranges 必须逐字节不变。
12. 失败、超时、进程崩溃、锁后外部修改均不能静默覆盖外部内容。
13. 迁移完成后旧 mtime guard、直接写盘和独立 hunk ledger 路径必须删除。

### 3.1 受管与非受管写入边界

受管写入包括 Edit、Write、项目 replace、hunk reject/undo、history restore 和 `/rewind`。这些入口必须经过 File Operations。

Bash/Terminal 可以执行任意第三方程序，IDE 和用户进程也不受 Mote 控制；它们属于非受管写入，不能诚实地宣称经过事务协调器。处理原则是：

- file watcher 使相关 snapshot/baseline/cursor 失效并记录 external attribution；
- 与受管事务重叠时，最终 identity + B0 compare 检出冲突；
- 不尝试用 monkey-patch、命令字符串分析或 LD_PRELOAD 伪装成完整拦截；
- 若未来需要让命令执行也成为受管事务，必须提供文件系统 sandbox/overlay，在命令结束后把完整 diff 作为 MutationSet 提交，而不是在 Bash 工具里猜测写了哪些路径。

## 4. Bounded context 与依赖方向

### 4.1 模块边界

新增正式 bounded context：

```text
contracts/fileops/
  models.py          # DTO、tagged union、结果与错误
  events.py          # durable transaction / review events
  ports.py           # 窄 Protocol

runtime/fileops/
  facade.py          # FileOperations 门面
  control.py         # 唯一 project operation admission/recovery 控制面
  fences.py          # transaction-indexed durable reservation set
  identity.py        # name/target/project identity
  snapshots.py       # sealed snapshot 与 artifact
  encoding.py        # 唯一编码决策和 byte mapping
  locking.py         # 层级跨进程 RW locks
  journal.py         # durable append、projection、reconcile
  publisher.py       # metadata-safe atomic replace/create/delete
  transactions.py    # 单文件和多文件 operation handler
  search.py          # 唯一搜索语义和结果存储
  recovery.py        # PREPARED 事务恢复
```

依赖保持：

```text
contracts <- runtime <- orchestration <- product
```

不新建 generic utils，不让 runtime import product。

### 4.2 对外端口

不是把整个 FileOperations 对象塞给工具，而是发布四个窄端口：

```python
class SnapshotReader(Protocol):
    def open_snapshot(...) -> FileSnapshot: ...

class SearchService(Protocol):
    def search(...) -> SearchResult: ...

class MutationCoordinator(Protocol):
    def prepare(...) -> PreparedMutation: ...
    def commit(...) -> MutationResult: ...

class ReviewService(Protocol):
    def status(...) -> HunkView: ...
    def transition(...) -> ReviewResult: ...
```

Role 经 `RoleComponents` 惰性装配 `_file_operations`；工具通过 `requires=(...)` 获得端口方法。新增 capability 同步进入 Role 白名单和 capability types。工具不得访问 RoleState、journal、blob store 或 lock registry。

### 4.3 产品层职责

Read/Search/Edit/Write 只负责：

- 参数 schema 和权限目标；
- 调用窄端口；
- 把结构化结果渲染成 `ToolResult`；
- 把大输出交给现有 tool-result limit。

它们不负责 open、stat、hash、decode、regex、snapshot、lock 或 rename。

## 5. 单一领域模型

### 5.1 PathToken

路径不能只用“假定 UTF-8 的 str”表示。引入可逆 `PathToken`：

```python
@dataclass(frozen=True)
class PathToken:
    display: str
    native: bytes | str
```

- Unix `native` 保留 `os.fsencode` bytes，展示使用 surrogateescape；
- Windows `native` 使用原生 Unicode 路径；
- 序列化时使用明确 tagged encoding，不 replacement decode；
- 所有工具结果和 cursor 使用 PathToken 的稳定 codec。

### 5.2 FileVersion

使用 tagged union，不用含义模糊的 optional fields：

```python
FileVersion = AbsentVersion | PresentVersion

@dataclass(frozen=True)
class AbsentVersion:
    name_identity: NameIdentity

@dataclass(frozen=True)
class PresentVersion:
    name_identity: NameIdentity
    target_identity: TargetIdentity
    size: int
    mtime_ns: int
    digest: str
    metadata_digest: str
```

`size/mtime_ns` 用于诊断和快速拒绝，安全判定使用 identity + SHA-256 + metadata policy。

### 5.3 FileSnapshot

```python
@dataclass(frozen=True)
class FileSnapshot:
    requested_path: PathToken
    target_path: PathToken
    project_identity: ProjectIdentity
    version: FileVersion
    artifact: BlobRef
    encoding: EncodingDecision | None
```

大文件 snapshot 可以是 content-addressed spool artifact，不要求常驻内存。消费者只读该 artifact，不再按原路径二次读取。

### 5.4 MutationSet

```python
Mutation = Create | Replace | Delete

@dataclass(frozen=True)
class MutationSet:
    transaction_id: str
    expected: tuple[FileSnapshot | AbsentVersion, ...]
    mutations: tuple[Mutation, ...]
    source: MutationSource
    recovery_policy: Literal["rollback_incomplete"]
```

Edit、Write、hunk reject、history restore、项目替换都生成同一类型；`/rewind` 使用同一 project barrier，但其工作树恢复由专用 publisher 执行。

## 6. Sealed snapshot：所有读取的公共基础

### 6.1 打开协议

```text
resolve requested path
→ open binary handle once
→ fstat(handle) 得到 identity/metadata S0
→ streaming read + SHA-256 + content-addressed spool
→ fstat(handle) 得到 S1
→ 验证 identity/size/mtime 未变化
→ 生成 immutable FileSnapshot
```

所有后续文本解码、PDF/Office 提取、图片读取、Search、Edit 分析都只读 snapshot artifact。

只在路径上 stat 后重新 open 不满足不变量。PDF visual 也必须指向 sealed artifact；不能在模型实际取媒体时重新读取可能已变化的原路径。

### 6.2 大文件与资源控制

- 流式 hash 和 spool，内存有固定上限；
- artifact 内容寻址去重；
- 每 session 和全局磁盘配额；
- 引用计数/事件可达性驱动 GC；
- 超配额在形成有效 snapshot 前失败，不返回半有效 revision；
- cancellation 删除未封存的临时 artifact。

### 6.3 Read state

RoleState 只保存 `path -> FileVersion + BlobRef` 的轻量引用，不保存 mtime-only state。API 直接替换为：

```text
record_file_snapshot(snapshot)
get_file_snapshot_ref(path)
invalidate_file_snapshots(project_identity)
```

旧 `record_file_read(path, mtime)` / `get_file_read_mtime()` 完整删除，不保留适配层。

## 7. 唯一编码与字节映射

### 7.1 EncodingDecision

Read/Search/Edit 共享一个决策器，顺序固定：

1. BOM：UTF-8 BOM、UTF-16 LE/BE、UTF-32 LE/BE；
2. 用户显式 encoding；
3. strict UTF-8；
4. 高置信探测候选；
5. 显式 fallback encoding；
6. strict decode + encode round-trip；
7. 无法可靠判定则返回 `Rejected`，建议 explicit encoding 或 hex。

不允许 `errors="replace"` 把未知或损坏 bytes 伪装成文本成功。

### 7.2 EditableTextSnapshot

```python
@dataclass(frozen=True)
class EditableTextSnapshot:
    raw: BlobRef
    text: str
    logical_to_raw_boundaries: tuple[int | None, ...]
    encoding: EncodingDecision
    bom: bytes
    newline_profile: NewlineProfile
```

替换时只编码 replacement fragment，再拼接未修改 raw byte ranges。保证：

- 原编码和 BOM 不变；
- mixed newline 的未修改部分不变；
- 未修改区域逐字节不变；
- replacement 无法编码时在 PREPARED 前失败；
- hunk geometry 同时携带 logical range 和 raw range。

## 8. 跨进程层级读写锁

### 8.1 为什么不用“project lock 最后拿”的特例

普通 Edit 与 `/rewind` 应遵循同一锁层级。project lock 使用 shared/exclusive 模式：

- 普通单/多文件 mutation：project shared lock；
- Read/Search：每次 sealed capture 持有短时 project shared lock，不跨整个 Search 长时间持锁；
- `/rewind`：project exclusive lock；
- name/target locks：exclusive。

统一顺序：

```text
所有 project locks（按 key 排序，shared/exclusive）
→ 所有 name locks（按 key 排序）
→ 所有 target locks（按 key 排序）
```

因此没有 `/rewind` 例外，也没有 `file → project` 与 `project → file` 的锁序反转。不同文件的普通 Edit 可在同一 project 下并发；rewind 等待全部 mutation 和正在 capture 的 shared holders 退出后独占发布。

### 8.2 单机锁引擎

锁实现集中在 runtime 的 `HierarchicalLockManager`，不是散落的 `FileLock` 调用：

- Unix：持久 lock file + `flock/fcntl` shared/exclusive；
- Windows：持久 lock file + `LockFileEx` shared/exclusive；
- 进程内：每 key 的可重入 owner/refcount 和公平等待队列；
- 跨进程状态由打开的 handle 持有，崩溃后 OS 自动释放；
- lock file 可长期存在，不通过删除文件解锁；
- 异步调用通过专用阻塞 IO executor，支持取消和 deadline；
- 锁目录权限仅当前用户可访问。

共享目录：

```text
~/.mote/runtime/file-locks/
  projects/<sha256>.lock
  names/<sha256>.lock
  targets/<sha256>.lock
```

实际根目录由 runtime path provider 生成，工具不硬编码。key 是稳定身份材料的 SHA-256，不泄露绝对路径。

`filelock` 可以继续服务其他模块，但 File Operations 不能以 exclusive-only `FileLock` 模拟跨进程 RW lock。

### 8.3 Identity

Name identity：

```text
canonical parent filesystem identity + normalized native filename
```

Target identity：

- Unix：device + inode；
- Windows：volume serial + file index；
- 其他平台必须有通过 capability probe 验证的稳定实现，否则启动时明确拒绝 managed mutation。

Project identity：

- Git 工作区使用 canonical repo root identity；
- 非 Git 使用配置的 workspace root identity；
- 一个 MutationSet 跨多个 project 时，获取排序后的多个 shared project locks；
- `/rewind` 只对自己的 project 取 exclusive。

### 8.4 symlink、hardlink 和特殊文件

- symlink：保留 link entry，target_path 指向真实文件；获取 requested name、target name 和 target identity locks；
- dangling symlink：拒绝；
- hardlink：`nlink > 1` 拒绝 atomic replace，避免静默断开 link 关系；
- directory/device/FIFO/socket：拒绝文本 mutation；
- create：锁 name identity，发布前再次确认 absent；
- delete：锁 name + target，发布前再次确认同一 B0。

### 8.5 唯一 Project Operation Control Plane

`ProjectOperationControl` 是锁、fence、准入、恢复分派和状态查询的唯一所有者。`MutationCoordinator`、`RewindCoordinator`、checkpoint store 和 snapshot reader 是 operation handler 或受控资源，不得各自实例化 fence store，不得调用 `assert_clear`，也不得从 facade 依次执行彼此独立的 recovery。

durable fence 不是单个 project 布尔位，而是按 transaction id 索引的 reservation set：

```text
recovery-fences/<project-key>/<sha256(transaction-id)>.json
```

每个 reservation 使用带版本的固定 schema：

```text
format_version
operation
transaction_id
session_id
authoritative journal path
artifact root
project lock mode
sorted name/target lock keys
```

journal 是事务状态和内容事实的唯一真相源；reservation 不复制 PREPARED/COMMITTED 状态。锁作用域是 PREPARED 之前 admission gap 所必需的 durable reservation，不是事务状态副本。未知 schema、不可逆路径、损坏 JSON 和 scope 不一致全部 fail closed，由 migrator 显式升级，运行路径不猜测。

普通 mutation 的准入状态机固定为：

```text
构造并排序完整 lock scope
→ 获取 project shared + name/target exclusive locks
→ 锁内重扫 reservation set
→ project-exclusive reservation：释放并进入 project recovery
→ 相交 mutation reservation：释放并进入 project recovery
→ 不相交 mutation reservation：允许并发
→ durable 写入自己的 scoped reservation
→ durable PREPARED
→ publish + verify
→ durable terminal event
→ durable 删除自己的 reservation
→ release locks
```

Read/Search 的单文件 capture 只获取 project shared；它们不被普通 mutation reservation 阻断，依靠 single-open sealed snapshot 得到明确版本，但遇到 rewind reservation 必须先恢复。Rewind/checkpoint 获取 project exclusive；锁内恢复并清理所有可收敛 reservation 后，fence set 必须为空，才能发布 project-exclusive reservation。

恢复只有一条路径：控制面获取 project exclusive，获取成功后在锁内重新扫描完整 fence set，再按 operation handler 从原 session 的权威 journal 对账。exclusive 获取会等待仍活着的 shared writer/capture 退出，因此不能把活跃 PREPARED 误判为崩溃事务。无 PREPARED 的 reservation 可清除；terminal event 的 reservation 可清除；PREPARED 根据 live == B0/B1/safety/target 收敛；无法证明时保留 reservation 并进入 IN_DOUBT。普通快速路径若 exclusive 当前被活跃操作占用，不得把“暂时拿不到锁”解释成崩溃，也不得清除 foreign reservation。

`FileOperations.health()` 从同一个控制面读取所有 project reservations 及其权威 journal 投影；不再分别拼接 mutation/rewind 的局部查询。控制面通过 handler registry 分派已封闭的 operation kind，新增 operation 必须同时提供 scope builder、reconciler 和 health projection，不允许在 facade 增加新的 `if operation == ...` 链。

### 8.6 单机部署边界

生产承诺只覆盖共享同一主机内核锁语义的线程和进程。POSIX 使用 `flock`，Windows 使用 `LockFileEx`；持锁进程退出后由内核释放 handle，durable journal/fence 负责重启恢复。当前设计不包含多主机协调，也不为未发生的需求预埋分布式 lease、fencing token、backend registry、feature flag 或自动降级路径。未来若产品边界真实扩展到多机，应作为新的架构决策重新定义一致性协议，而不是污染当前单机正确性模型。

## 9. Durable 文件事务

### 9.1 单一真相源

`rollout.jsonl` 继续是 session 历史的崩溃安全真相源。新增 typed events：

```text
FileTransactionPrepared
FileTransactionCommitted
FileTransactionAborted
FileTransactionInDoubt
HunkReviewTransitioned
RewindPrepared
RewindCommitted
RewindAborted
RewindInDoubt
```

before/after bytes 存现有 content-addressed blob store，事件只保存 blob refs、versions、metadata 和 transaction id。

删除独立的 hunk status JSONL 真相源。hunk 列表和状态是 rollout 单次正向 replay 的 projection；UI 查询只读 projection，状态变更重新从 durable tail 验证 expected version。

### 9.2 DurableEventAppender

普通异步 EventBus subscriber 不能充当发布前 barrier。runtime 提供窄端口：

```python
append_and_sync(event) -> DurablePosition
read_latest(projection_key) -> VersionedState
```

- 同 session append 有跨进程 journal lock；
- 每条 JSONL event 是一次完整 append；
- append 后 fsync 才返回；
- durable append 成功后再向观察平面发布；
- replay 仍是单次正向扫描；
- compaction/checkpoint 不丢失未终结 transaction 和 review projection。

### 9.3 Prepare

```text
解析并冻结完整 MutationSet
→ 获取排序后的 project/name/target lock set
→ 锁内重新 sealed-read 所有 B0
→ 对比调用方 expected FileVersion
→ 生成所有 B1 和 metadata plan
→ durable put(B0) + durable put(B1)
→ append_and_sync(FileTransactionPrepared)
```

任一 snapshot/blob/journal/fsync 失败：释放锁并返回失败，磁盘目标零变化。

### 9.4 Publish

每个目标发布前重新验证：

- name identity；
- target identity；
- metadata policy；
- current bytes == B0；
- hardlink/symlink invariant。

之后调用唯一 `AtomicPublisher`。所有目标完成后 append-and-sync 一个 `FileTransactionCommitted`，其中包含可由 B0/B1 确定性生成的 hunk records。只有 committed event 成功后，工具才返回 success 并刷新 RoleState projection。

### 9.5 崩溃恢复

resume 时先扫描未终结 PREPARED transaction，在获取同一 lock set 后比较 live state：

| live state | 处理 |
| --- | --- |
| 全部等于 B0 | 记录 Aborted；没有发布 |
| 部分/全部等于 B1，其余等于 B0 | 按 `rollback_incomplete` 恢复全部 B0，再记录 Aborted |
| 全部等于 B1 | 补记 Committed，不重复写 |
| 任一既非 B0 也非 B1 | 记录 InDoubt，不覆盖外部内容，要求显式处理 |

恢复是 idempotent；同一 transaction 可重复运行，不产生重复 hunk。

### 9.6 多文件语义

普通文件系统无法让多个 pathname 在一个瞬间同时切换，因此不声称多文件瞬时原子可见。Mote 提供的是：

- 写前冻结完整候选和 blast radius；
- 全 lock-set 排序持有；
- 全体 B0/B1 和 PREPARED 先 durable；
- 发布失败或进程崩溃时确定性补偿回 B0；
- 最终只产生 Committed 或 Aborted；
- 外部非协作写入导致 InDoubt，绝不覆盖。

在 Mote 协作参与者之间，锁使中间状态不可见；非协作进程理论上可能在短暂发布窗口观察到部分 B1，这是普通文件系统的物理边界。

## 10. AtomicPublisher

唯一 publisher 支持 Create/Replace/Delete：

### 10.1 Replace/Create

```text
same-directory create_new random temp
→ 写完整 B1
→ 应用并验证 PreservedMetadata
→ flush + fsync temp
→ platform atomic replace
→ fsync parent directory
```

要求：

- temp 名使用 cryptographic random/sequence + `O_EXCL`，不只使用 PID；
- `try/finally` 清理未发布 temp；
- Unix 保留 mode、owner/group 和明确支持的 xattrs/ACL；
- Windows 使用保留 security descriptor/attributes 的平台 replace API；
- metadata 无法按 policy 保留时在 replace 前失败；
- 新建文件权限遵守安全 policy 和 umask；
- 错误区分 temp-create、write、metadata、fsync、replace、dir-fsync。

不要笼统声称跨所有文件系统保留全部 metadata。支持矩阵由平台 capability test 生成；不满足 mutation 所需 policy 时 fail closed。

### 10.2 Delete

Delete 也是事务 mutation：

- B0 blob 与 PREPARED 已 durable；
- final B0 compare 后原子 rename 到同目录私有 tombstone；
- fsync parent；
- transaction committed 后清理 tombstone；
- 清理失败由 GC 重试，不影响逻辑删除；
- crash recovery 可从 tombstone/B0 恢复。

## 11. 现有恢复能力的迁移

### 11.1 Snapshot/history

当前写入的历史事实统一来自 `FileTransactionPrepared/Committed`：

- history 列表投影 committed transactions；
- before-image 就是 PREPARED 的 B0 blob；
- restore 生成新的 MutationSet，因此 restore 本身也可撤销；
- 不再由工具额外调用 `_snapshot_pre_write()`；
- snapshot 不是 best-effort hook，而是 transaction prepare 的硬前置条件。
- schema v1 的旧 `file_snapshot` 只在一次性 migrator 中转为 `FileHistoryImportedEvent`，仅保留旧记录能够证明的 before-image；它不是伪造的 transaction。

### 11.2 Hunk review

Committed event 持有由 B0/B1 确定性生成的 hunk ids、logical ranges、raw ranges、encoding 和 pre/post versions。

状态迁移：

```python
transition(
    hunk_id,
    expected_version,
    target=ACCEPTED | REJECTING,
) -> ReviewResult
```

accept：

```text
project shared + file locks
→ durable re-query hunk projection/version
→ 验证 live post-version
→ append_and_sync(accepted transition)
→ 更新派生 baseline projection
```

reject/undo：

```text
durable re-query pending
→ 从 raw ranges 构造 revert MutationSet
→ append_and_sync(rejecting + child transaction id)
→ 走统一 MutationCoordinator
→ append_and_sync(rejected)
```

崩溃时 child transaction reconciliation 决定 live 是 pre/post/in-doubt，再完成对应 review transition。不存在“文件已 reject、状态仍 pending、下一进程重复 reject”的窗口。

批量 reject 按文件分组、从高 raw offset 到低 offset 生成一个 MutationSet；不是循环调用单 hunk reject。

### 11.3 `/rewind`

`/rewind` 保留独立 Git object DB 和 checkpoint 语义，但发布受 project exclusive lock 保护：

```text
project exclusive lock
→ durable re-query checkpoint
→ 捕获 rewind 前 safety checkpoint
→ durable RewindPrepared
→ read-tree/reset/clean
→ durable RewindCommitted
→ invalidate project snapshots/baselines/search cursors
→ release
```

普通 mutation 在整个事务期间持有 project shared lock，因此不能与 rewind 交错；不需要特殊锁顺序或最后一刻碰运气。

checkpoint capture/restore 失败不再吞掉后继续报告成功。受管 rewind 的 durability barrier 失败即失败关闭。

RewindPrepared 必须记录 rewind 前 safety checkpoint、目标 checkpoint、目标 project identity 和恢复策略。resume 时重新获取 project exclusive lock：live tree 等于目标 tree 时补记 committed，等于 safety tree 时补记 aborted；两者都不等时记录 in-doubt，不覆盖进程退出后出现的外部修改。任何新 mutation 在该 project 的 rewind 对账结束前不得开始。

OS lock 会在进程崩溃后自动释放，因此仅有 session-local PREPARED 不足以阻断其他 session；这个问题同时存在于普通 mutation 和 rewind。跨 session 阻断、并发准入和恢复全部使用 8.5 定义的 transaction-indexed durable reservation set。禁止重新引入单个 `ProjectRecoveryFence`、project-global `assert_clear` 或 coordinator-local recovery；它们无法同时表达不相交 mutation 并发、PREPARED 前窗口和 project-exclusive rewind。

## 12. 统一 Search 引擎

### 12.1 一个 regex 语义

不再让 ripgrep 搜正文、Python `re` 搜富文档。新 SearchEngine：

1. 用 ripgrep `--files -0` 或等价 walker 只做 candidate discovery 和 ignore policy；
2. 每个 candidate 通过 SnapshotReader 冻结；
3. 文本和富文档都生成统一 `SearchableText`；
4. 全部交给同一个 `RegexProgram.finditer()`；
5. count、content、only_matching、files_with_matches、summary 共用同一 match stream。

ripgrep 不再参与正文结果协议，因此没有 `path:line:text` 解析、`rg -c` 语义和 stdout replacement decode。

### 12.2 SearchRow

```python
@dataclass(frozen=True)
class SearchRow:
    path: PathToken
    version: PresentVersion | None
    line_number: int | None
    text: str
    matched_text: str
    occurrence_count: int
    is_context: bool
```

name-only discovery 不读取正文，因此 `version=None`；所有正文 row 都携带 sealed `PresentVersion`。富文档当前提供统一 line provenance，PDF page provenance 在页级 extractor 阶段补齐。

### 12.3 完整结果和稳定分页

一次 query 生成 immutable result artifact：

```python
SearchResult:
    status: Complete | Partial
    rows: tuple[SearchRow, ...]
    artifact: BlobRef
    summary: SearchSummary
    skipped: tuple[SkippedFile, ...]
    next_cursor: str | None
```

- `rg --sort path -0` 的候选输出先进入 bounded spool，再逐条消费，不进入 Python 全量列表；
- rows 和 skipped-file report 分别逐条写 content-addressed NDJSON blob；manifest 只保存 blob refs、row count、summary、最多 100 条 skipped preview 和 rendering semantics；
- cursor 包含版本、manifest artifact id 和 row offset，并严格校验 SHA-256/size，不能构造 artifact path traversal；
- 下一页只读同一 artifact，不重新扫描；
- artifact 过期明确报错，不混入新版本文件；
- 默认页为 1000 rows，`None/0` 不再意味着无界内存；
- summary totals 来自完整 scan；deadline 时 `complete=false, termination=timeout`，不能把局部 totals 称为完整；
- 排序使用 ripgrep stable path order + source position，PathToken native bytes 通过 codec 无损进入 artifact。

skipped reason 至少包括 binary、encoding rejected、permission、extractor unavailable、extract failed、changed while snapshotting、limit、cancelled。

### 12.4 一致性边界

每个文件的 SearchableText 都来自单一 sealed version。通用文件系统没有跨目录全局 snapshot，因此一次 project scan 不是全树同一瞬间快照；结果显式携带每个 source revision。这比隐式混合时间点可审计，也不伪造不存在的全局一致性。

## 13. Read、PDF 与 raw/hex

Read 只消费 FileSnapshot：

- text：统一 encoding，明确 `Complete | Partial` 和 next logical offset；
- raw：byte offset + byte limit，返回 base64/artifact，不冒充 Unicode；
- hex：byte offset、hex、ASCII gutter、next byte offset；
- PDF text：选择 pages，保留 page boundary/provenance；
- PDF visual：原始 sealed PDF artifact；
- PDF render：指定 pages 渲染 PNG，支持只接受图片的模型；
- Word/Excel/Notebook/video/image：extractor 接收 artifact/bytes，不重开原路径。

所有分页基于 immutable artifact，因此续读不会因源文件变化发生重复或遗漏。

文本路径已经按该协议落地：首次 partial page 持久化
`{FileSnapshot, materialized-text BlobRef, mode}` manifest，opaque cursor 只引用该
content-addressed manifest 与下一逻辑行；cursor 恢复时同时校验 manifest、raw
snapshot artifact、materialized text artifact 和 requested path。`next_offset` 仅用于
诊断展示，不再作为一致性令牌。Search 与 TextView 都只能调用
`TextSourceService`，后者是唯一允许依赖 `decode_text`、document registry 和
`ManagedSnapshotCapture` 的文本物化入口。

text/raw/hex/PDF text/PDF render 现共享一个 tagged immutable Read manifest 协议，
Read 与 Search 的 continuation 则统一由 `DurableCursorRegistry` 管理。cursor token
只是固定长度 HMAC grant，不携带 manifest、artifact 或 position；registry durable
记录 namespace、root manifest、position、timeline epoch、idle/hard TTL 和完整 pin
closure。相同 token/position 的 advance 跨进程幂等地产生相同 token，namespace 不可
混用，rewind epoch 变化后旧 grant 在读取 manifest 前即被拒绝。`next_offset` /
`next_pages` 仍只用于诊断展示，不能作为 continuation 输入。

## 14. Edit 与项目级替换

### 14.1 单文件 Edit

Edit 从用户最后 Read 的 FileSnapshotRef 开始：

```text
读取 snapshot artifact
→ 统一编码/byte mapping
→ substring/regex match plan
→ 生成 B1 raw bytes
→ MutationCoordinator.prepare + commit
```

支持 literal、regex、capture groups、单次/全量和 whole-file replacement，但全部映射到同一 Mutation。

### 14.2 项目级 replace

```text
discover 完整候选
→ sealed snapshot 所有候选
→ 统一 regex 分析
→ 冻结 matches/replacements/blast radius
→ 校验全局 max_replacements 和资源 limits
→ 生成可持久化 dry-run plan
→ 用户/调用方提交同一 plan id
→ revalidate plan snapshots
→ 单个 MutationSet prepare/publish/reconcile
```

dry-run 与 commit 使用同一 immutable plan，不在 commit 时重新解释 regex。plan 过期则整体返回 stale，要求重新预览。

项目事务成功返回 Committed；内部失败并成功补偿返回 Aborted；遇到非协作外部写入返回 InDoubt。普通预期冲突不再以“前几个文件已经永久提交”的 `Partial` 作为正常语义。

## 15. 高可用与可观测性

### 15.1 失败分类

所有结果使用 typed errors：

- `StaleSnapshot`；
- `IdentityChanged`；
- `ContentChanged`；
- `LockTimeout` / `LockCancelled`；
- `EncodingRejected`；
- `SnapshotDurabilityFailed`；
- `JournalDurabilityFailed`；
- `MetadataPreservationFailed`；
- `PublishFailed`；
- `RecoveryInDoubt`；
- `UnsupportedFilesystemSemantics`。

产品层只翻译，不靠字符串解析决策。

### 15.2 健康状态

FileOperations 暴露只读 health：

- lock backend capability；
- journal writable/fsync status；
- blob store writable/readable status；
- recovery backlog；
- in-doubt transactions；
- artifact quota/GC pressure；
- platform metadata support。

有未对账 PREPARED 时，相交 scope 的新 mutation 先恢复后执行；不相交 mutation 可在同一 project 并发。project-exclusive rewind reservation 阻断整个 project。InDoubt mutation scope fail closed，但不阻塞同 project 的无关 lock scope；InDoubt rewind 阻断整个 project。

### 15.3 日志与指标

关键类使用 `@log_class`，不手写散乱 inline logger。指标至少包括：

- snapshot bytes/latency/dedup ratio；
- lock wait/timeout/contention；
- transaction prepare/publish/reconcile latency；
- stale/conflict/in-doubt count；
- atomic publish failure stage；
- search scanned/skipped/result bytes；
- artifact quota/GC。

日志和事件携带 transaction id、session id、project key、匿名 lock key；不记录敏感完整内容。

## 16. Schema 与迁移纪律

### 16.1 事件版本

rollout 增加明确 schema version。旧 session 通过集中式 migrator 一次转换为新 typed events：

- 原 `file_snapshot` 转为正式 `FileHistoryImportedEvent`，不虚构 transaction commit；
- 原 hunk ledger 合并为 HunkDetected/HunkReviewTransitioned events；
- 原 checkpoint 保留；
- migration 先写新文件并 fsync，再原子替换；原件保留只读备份直到验证完成。

runtime 核心只处理当前 schema，不在业务路径散布 `if old_field`。版本迁移是正式基础设施，不是永久双实现。

### 16.2 代码迁移完成条件

同一个主干提交序列中完成所有 managed writers 切换，最终必须删除：

- `get_file_read_mtime` / mtime-only RoleState；
- `FileMutatingTool` 中直接 open/write 和 guard；
- `_snapshot_pre_write()`；
- 独立 hunk status JSONL 写路径；
- history restore 的直接 temp/replace；
- hunk reject 的直接 `atomic_write`；
- Edit/Write 的 unbound safety bypass；
- Search 的 `path:line:text` parser；
- `rg -c` 和富文档独立 regex 分支；
- 所有 `errors="replace"` 的文件内容/路径协议。

测试 helper 也必须经正式端口或显式 in-memory backend，不允许保留 production bypass 只为旧测试通过。

## 17. 实施序列

以下序列已经实施完成，保留为架构验收轨迹。每一阶段以架构不变量和删除清单为出口条件，不以“新类已经存在”为完成。

### Phase A：领域内核

- contracts/fileops DTO、events、ports、typed errors；
- PathToken、identity、FileVersion；
- sealed SnapshotReader 和 artifact quota；
- 统一 EncodingDecision/EditableTextSnapshot；
- 单机平台锁实现 + 层级 LockSet；
- AtomicPublisher 与 metadata capability tests。

出口：内核可独立完成 sealed read、锁、prepare、atomic publish，故障注入测试通过。

### Phase B：durable transactions

- DurableEventAppender；
- prepared/committed/aborted/in-doubt events；
- MutationCoordinator；
- 唯一 ProjectOperationControl；
- scoped durable reservation set 与 operation handler registry；
- 多文件补偿；
- resume reconciliation；
- FileOperations health。

出口：进程在每个 fsync/replace 窗口被 kill 后，任意 session 均经唯一控制面收敛到 committed/aborted/in-doubt；不出现无记录成功写入，不相交 mutation 可并发，相交 scope 与 rewind 不可穿透，生产代码不存在 coordinator-local fence/gate。

### Phase C：所有 managed writers 一次迁移

- Edit、Write；
- snapshot/history restore；
- hunk accept/reject/undo；
- `/rewind` project RW barrier；
- Role capability/state；
- 删除全部旧直接写盘路径和 mtime API。

出口：架构测试扫描 product/runtime，除 AtomicPublisher 和明确 journal/blob IO 外，没有受管目标的直接写入。

### Phase D：统一 Search/Read

- candidate discovery；
- 单一 regex engine；
- SearchableText/SearchRow；
- file-backed immutable results/cursor；
- raw/hex；
- PDF pages/render；
- 全部 extractor artifact 化；
- 删除旧 ripgrep content protocol 和双语义分支。

### Phase E：项目级 replace 与迁移工具

- immutable edit plan；
- regex/capture/glob/max limit；
- multi-file MutationSet；
- rollout/hunk schema migrator；
- 旧 session 样本迁移验证；
- 完整性能、容量和长时 soak 测试。

## 18. 测试矩阵

### 18.1 锁

- multiprocessing 同 name、同 inode、symlink alias 竞争；
- project shared 并发和 rewind exclusive 公平性；
- 同 project 不相交 reservation 并发、相交 reservation 恢复后重试；
- fence durable 但 PREPARED 尚不存在时按 scope 准入；
- active PREPARED 存在时 recovery exclusive 必须等待，不能误清 fence；
- 多 project/multi-file lock-set 排序无死锁；
- 持锁进程 `os._exit` 后自动释放；
- timeout/cancel 不泄漏进程内 owner/refcount；
- Windows path normalization；
- backend capability probe 失败时 fail closed；

### 18.2 Snapshot/version

- read 期间 in-place mutation；
- read 期间 inode replacement；
- 相同 mtime 不同 bytes；
- 相同 bytes 不同 identity；
- 大文件 streaming、cancel、quota、GC；
- PDF/Office extractor 和 visual 消费 sealed artifact；
- source 改变后续页仍来自原 artifact。

### 18.3 Transaction/crash

逐个 barrier 注入真实进程崩溃：

- B0 blob 后、B1 blob 前；
- blobs 后、PREPARED fsync 前/后；
- 每个文件 replace 前/后；
- parent fsync 前/后；
- 全部 publish 后、COMMITTED fsync 前；
- COMMITTED 后、RoleState projection 前；
- compensation 每一步；
- tombstone rename/cleanup；
- recovery 再次崩溃。

每个 case 从磁盘重建，不能用普通异常代替进程 crash。

### 18.4 外部竞争

- IDE/Bash 在分析、锁等待、final compare、replace 窗口修改；
- 外部原子替换为相同 bytes；
- 外部修改 metadata/xattr/ACL；
- external state 等于 neither B0 nor B1 时进入 InDoubt；
- InDoubt 只隔离相关 target/project，不拖垮无关写入。

### 18.5 Recovery/review

- 两进程同时 accept/reject 同 hunk；
- stale projection 下 durable expected-version 失败；
- reject child transaction 每个 crash 窗口；
- batch reject raw offset 顺序；
- history restore 可再次撤销；
- rewind 与单/多文件 mutation 竞争；
- rewind 后 snapshots/baselines/cursors 失效；
- migrated legacy session 的 history/hunk/checkpoint 等价。

### 18.6 Encoding

- UTF-8/BOM、UTF-16/32 LE/BE、GBK、Big5、Shift-JIS；
- 模糊/非法编码拒绝；
- replacement 不可编码时零事件、零写入；
- mixed newline 未修改 ranges byte-equal；
- edit → reject → history restore 后 raw bytes 等于原件；
- BOM 和 encoding across project replace；
- raw/hex 在多字节字符中间分页。

### 18.7 Search

- 一行多个 occurrence；
- zero-width、multiline、capture、case folding；
- 普通文本与富文档使用同一 regex program；
- path 含冒号、换行、非 UTF-8 bytes、Windows drive；
- `.gitignore`、hidden、explicit file 规则；
- extractor unavailable/failure skipped；
- immutable cursor 无重复/遗漏；
- cancelled/timeout summary 明确 incomplete；
- 百万级结果内存上限、artifact 配额与 GC。

### 18.8 架构测试

- contracts 不依赖上层；
- runtime/fileops 不依赖 product；
- product 文件工具不得直接 import `open/os.replace/atomic_write` 写目标；
- managed writer 只能依赖 MutationCoordinator；
- 不存在旧 mtime capability；
- 不存在独立 hunk ledger writer；
- Search 不解析冒号协议、不调用 `rg -c`；
- 文件内容协议不使用 replacement decode；
- 所有新 events 在 replay/migration 有覆盖。

## 19. 最终验收描述

完成后，Mote 的准确定位应是：

- 富文档、多媒体和模型上下文能力继续由产品层提供；
- File Operations 是 Read/Search/Edit/恢复共同依赖的长期内核；
- 每次受管读取都有 sealed version，每次受管写入都有 durable B0/B1 和事务事实；
- 跨线程、跨进程、路径别名和 rewind 使用同一层级锁协议；
- snapshot、hunk、history 和 crash recovery 是同一 transaction log 的投影；
- traditional encoding、BOM、newline 和未修改 bytes 可验证保真；
- Search 只有一个 regex/count/path/pagination 语义；
- 多文件修改在协作参与者间隔离，失败可确定性补偿，对物理上无法保证的外部瞬时可见性如实陈述；
- 旧实现、旁路和兼容残渣已删除。

这不是在现有工具上逐项打补丁，而是建立一个可长期演进、可故障恢复、可审计并且边界诚实的文件操作平台。
