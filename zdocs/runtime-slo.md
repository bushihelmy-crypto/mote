# Mote Runtime SLO

本文件定义本地 durable flow 的参考服务等级目标。权威数值位于 `flow/slo.py`，测试不得复制常量。

| 指标 | 默认目标 | 验证 |
| --- | ---: | --- |
| 10,000 条 journal 冷恢复 | `< 2s` | 从真实 JSONL 重建并 fold 全量索引 |
| 10,000 次纯 Graph 转移 | `< 1s` | 不含领域 I/O 的控制平面基线 |
| 1,000 条排队写入的磁盘 barrier | `< 5s` | `DiskWriter.drain()` 后逐条可读 |
| 结构化关闭 | `< 5s` | maintenance、watcher、title task 必须 cancel + await |
| public RunEvent 缓冲 | `≤ 256` | 有界队列；慢消费者通过 `await put` 施加背压 |

这些是 CI 参考硬门槛，不是对模型、网络、外部工具延迟的承诺。生产监控应分别统计 journal rebuild、disk barrier、shutdown 和各 `RunPhase` 延迟；超过目标应告警，不能通过扩大无界缓存隐藏压力。

崩溃正确性不使用概率指标：EXTERNAL unknown-after-crash 禁止自动重放、tool-call/result 必须配对、accepted/committed output 必须可恢复，均为 100% 契约，由 `ztest/flow/durable/test_process_crash_recovery.py` 验证。
