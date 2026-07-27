# Runtime 生命周期与资源所有权

Mote 只有一个关闭协议：[`LifecycleStack`](../runtime/lifecycle.py)。资源仍按作用域分为
session 与 shared 两层，不因关闭协议统一而提升为进程单例。

## 所有权

| 所有者 | 资源 |
| --- | --- |
| `Role` | file watcher、background tasks、EventBus、MCP、tool runtimes、LSP、Sandbox、managed runtimes、maintenance、repo index、session log |
| `EventBus` | 实现 `AsyncCloseSubscriber` 的 title、LSP、tracing 等 subscriber |
| `Context` | Provider clients、Langfuse client/exporter、DiskWriter |
| `EngineServices` | Context 与显式注册的共享扩展资源 |
| `Engine` | 所有由该 Engine 创建且尚未 release 的 Agent，以及 EngineServices |
| `BaseProjector` | CLI/服务端输出 consumers 与其 transport |
| `SessionDriver` | scheduler、input port、projector、control plane、Engine |
| `SessionRegistry` | 每个 resident session 的 control plane 与 Engine Agent ownership |

`MCP`、`LSP` 和 `Sandbox` 是 session 私有资源。它们由 Engine 关闭，是因为 Engine 先关闭
Role；不是因为它们被共享到 Engine 上。

## 阶段

`LifecyclePhase` 定义稳定的关闭屏障：

1. `STOP_PRODUCERS`
2. `CLOSE_RESOURCES`
3. `FLUSH_EXPORTERS`
4. `FLUSH_DURABILITY`
5. `RELEASE_CONTAINER`

阶段按升序执行，同阶段按注册逆序执行。同阶段某个资源失败时，其余同阶段资源仍会全部
尝试；后续阶段不会提前执行。成功资源从栈中移除，失败资源保留，下一次 `aclose()` 只重试
失败资源，再继续后续阶段。

因此 Provider 未关闭时 exporter 和 DiskWriter 不会被提前销毁；exporter 未 flush 时
DiskWriter 也不会提前关闭。Role 私有资源未全部关闭时，`EngineServicesLease` 不会释放。

## 并发与取消

`aclose()` 的首个调用创建唯一关闭任务，其他调用者等待同一任务。等待者取消通过
`asyncio.shield()` 隔离，不会取消实际资源回收。关闭成功后重复调用是 no-op；关闭失败后
可安全重试。

## 扩展资源

共享扩展必须以命名 `LifecycleResource` 注册到 `EngineServices` 或 `Context`，并选择明确的
`LifecyclePhase`。不要把关闭回调散落到 CLI、factory 或模块退出钩子，也不要依赖对象析构器。

EventBus subscriber 若拥有后台任务、连接或 exporter handle，必须显式实现
`AsyncCloseSubscriber`。普通无状态 observer 不需要空的关闭方法。

服务端 eviction 只有在 session 生命周期栈成功关闭后才从目录移除 session；失败 session
继续驻留，下一次 eviction/aclose 只重试失败资源。这样服务端不会把“目录里已经消失”误当成
“资源已经关闭”。
