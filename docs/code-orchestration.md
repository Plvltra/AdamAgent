# B14.1：隔离脚本与注册工具之间的窄 RPC

已实现独立 API：`IsolatedOrchestrator.run(code, invoke)`。Python 脚本在 [B19.1 隔离后端](runtime-isolation.md) 中运行，通过 `call_tool(name, arguments)` 请求宿主，脚本的 `result` 变量成为最后的有界返回值。循环、列表筛选和聚合都在 worker 内完成，中间工具结果不必逐项回传给模型。

当前尚未注册模型的代码编排工具，也没有接入主聊天轮次。**聊天的工具数、请求来源、Hook 前后置、附件、计划证据、录制与会话日志必须在 B14.2 接入原有执行链；B14.1 不声称这些连接已完成。** B14.3 再做生产闭环、超额、中断及真实进程故障组合验收。

## 模块职责

| 模块 | 责任 |
| --- | --- |
| [rpc.py](../adam/isolation/rpc.py) | 宿主协调器；探测隔离、读取请求、在当前线程调用宿主回调，核对完成事实并清理通道 |
| [rpc_protocol.py](../adam/isolation/rpc_protocol.py) | 有界 JSON 帧；格式、深度、节点数、唯一顺序号和最多 16 次调用 |
| [linux_rpc.py](../adam/isolation/linux_rpc.py) | 可信 Linux 转发器；只转发一问一答，监督隔离进程，不执行工具 |
| [linux_worker.py](../adam/isolation/linux_worker.py) | 复用固定命名空间、只读运行时、两阶段内核过滤和资源硬限额 |
| [code_bridge.py](../adam/tool_calls/code_bridge.py) | 显式 API 的注册工具桥；宿主选择允许名称，沿用 ToolRegistry 参数、模式、路径和可选前置回调 |

`RegisteredToolBridge` 只接受已注册且不重复的工具名称。脚本不能扩大列表、修改挂载或拿到处理函数；未开放工具返回明确失败。文件内容实际由宿主原工具读取，worker 无法直接读取 Windows 磁盘；命令和编辑若以后开放，仍需原工具的确认机制。没有为 B14 新造绕过这些规则的文件或命令实现。

回调仍在发起编排的协调线程执行，可复用原来要求同线程的会话和批准设施。通道等待采用 asyncio；此同步 API 不应在已运行的 asyncio 事件循环中调用。回调发生存储或取消异常时停止整次编排，不自动重发可能已产生副作用的请求。

## 协议与限制

宿主发送一次代码，worker 发出带连续序号的 `call`，宿主只回复对应 `reply`；worker 最后返回 `result`，可信启动器核对退出后发送 `finished`。worker 伪造宿主结束帧、重复调用、并行发多个请求或在回复前结束都会失败。

每帧最多 64 KiB、JSON 深度 32、节点 10,000；拒绝重复键、非有限数、超长整数和残缺尾帧。初始代码最多 64 KiB，最终结果帧最多 8,000 字节；普通 print 转到有界 stderr。输出不是任意宿主代码或 shell 指令。

继承 256 MiB 地址空间、2 秒 CPU、无派生进程/线程、无网络、不可再次 exec 的内核限制。计算阶段累计最多 5 秒；等待宿主工具期间暂停这项计时，但每次等待最多 60 秒、总墙钟最多 300 秒，CPU 等硬限额不暂停。宿主通道自身还有 330 秒等待边界，业务回调必须沿用自己的取消/超时约束。

返回包含真实退出码、结束原因、宿主实际派发次数及 `has_result`。失败不得把 `None` 当成成功摘要；协议断开时实际工具动作可能已经发生，所以只能报告结果未知并停止，不能自动重跑。B14.2 需将这些事实纳入会话记录。

## 已有验证

[17 项业务/协议测试](../tests/features/test_code_orchestration.py) 使用真实临时文件和原注册工具，检查路径授权、参数、规划模式、未开放工具、取消、前置回调及错误不重试；加上 B19.1 的宿主契约及架构检查共 63 项通过。协议替身不能证明真实 OS 隔离。

[真实隔离报告](evaluations/b14-rpc.json) 另行通过三项检查：

1. `file_info → read_file → worker 筛选`，只返回文件字节数与筛出的 `gamma`；两次真实工具回调均在原协调线程。
2. 请求未开放的 `run_command` 被拒绝，没有启动宿主命令。
3. worker 直接查看 `/mnt/c` 不可见，创建网络 socket 被内核拒绝。

另以 `--backpressure` 保存真实故障：worker 发出两次请求后故意不读取每份约 60 KiB 的回复，非阻塞写入让外部看守继续运行，在等待上限后停止。报告记录实际调用数和从首次回调开始的耗时；不因管道满而失去超时能力。

报告只保存最终摘要，不保存临时绝对路径或中间完整文件返回。复现入口：

```powershell
python -m scripts.orchestration_acceptance --backpressure --output rpc-result.json
```

输出必须不存在；没有隔离环境会失败，不替换成 Mock，也不消耗模型 API。
