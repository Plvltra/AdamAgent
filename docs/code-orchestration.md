# B14：隔离代码编排与原工具执行链

已实现独立 API：`IsolatedOrchestrator.run(code, invoke)`。Python 脚本在 [B19.1 隔离后端](runtime-isolation.md) 中运行，通过 `call_tool(name, arguments)` 请求宿主，脚本的 `result` 变量成为最后的有界返回值。循环、列表筛选和聚合都在 worker 内完成，中间工具结果不必逐项回传给模型。

B14.2 已将 execute_code 接入主/子应用、原工具执行链、共享额度、来源与录制。默认关闭，显式开启后仍需实际隔离后端。B14.3 的越权、超额、中断及真实进程故障组合验收尚未完成。

## 模块职责

| 模块 | 责任 |
| --- | --- |
| [rpc.py](../adam/isolation/rpc.py) | 宿主协调器；探测隔离、读取请求、在当前线程调用宿主回调，核对完成事实并清理通道 |
| [rpc_protocol.py](../adam/isolation/rpc_protocol.py) | 有界 JSON 帧；格式、深度、节点数、唯一顺序号和最多 16 次调用 |
| [linux_rpc.py](../adam/isolation/linux_rpc.py) | 可信 Linux 转发器；只转发一问一答，监督隔离进程，不执行工具 |
| [linux_worker.py](../adam/isolation/linux_worker.py) | 复用固定命名空间、只读运行时、两阶段内核过滤和资源硬限额 |
| [code_bridge.py](../adam/tool_calls/code_bridge.py) | 显式 API 的注册工具桥；宿主选择允许名称，沿用 ToolRegistry 参数、模式、路径和可选前置回调 |
| [orchestration.py](../adam/contracts/orchestration.py) | CodeRunner 窄接口与有界执行结果，不依赖 OS 实现 |
| [orchestration_tools.py](../adam/tool_calls/orchestration_tools.py) | 模型工具说明、代码参数校验与执行结果；bootstrap 注入真实后端及原执行器回调 |

`RegisteredToolBridge` 只接受已注册且不重复的工具名称。脚本不能扩大列表、修改挂载或拿到处理函数；未开放工具返回明确失败。文件内容实际由宿主原工具读取，worker 无法直接读取 Windows 磁盘；命令和编辑若以后开放，仍需原工具的确认机制。没有为 B14 新造绕过这些规则的文件或命令实现。

回调仍在发起编排的协调线程执行，可复用原来要求同线程的会话和批准设施。通道等待采用 asyncio；此同步 API 不应在已运行的 asyncio 事件循环中调用。回调发生存储或取消异常时停止整次编排，不自动重发可能已产生副作用的请求。

## 协议与限制

宿主发送一次代码，worker 发出带连续序号的 `call`，宿主只回复对应 `reply`；worker 最后返回 `result`，可信启动器核对退出后发送 `finished`。worker 伪造宿主结束帧、重复调用、并行发多个请求或在回复前结束都会失败。

每帧最多 64 KiB、JSON 深度 32、节点 10,000；拒绝重复键、非有限数、超长整数和残缺尾帧。初始代码最多 64 KiB，最终结果帧最多 8,000 字节；普通 print 转到有界 stderr。输出不是任意宿主代码或 shell 指令。

继承 256 MiB 地址空间、2 秒 CPU、无派生进程/线程、无网络、不可再次 exec 的内核限制。计算阶段累计最多 5 秒；等待宿主工具期间暂停这项计时，但每次等待最多 60 秒、总墙钟最多 300 秒，CPU 等硬限额不暂停。宿主通道自身还有 330 秒等待边界，业务回调必须沿用自己的取消/超时约束。

返回包含真实退出码、结束原因、宿主实际派发次数及 `has_result`。失败不得把 `None` 当成成功摘要；协议断开时实际工具动作可能已经发生，所以只能报告结果未知并停止，不能自动重跑。B14.2 已将返回事实与底层来源纳入会话记录。

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

## B14.2.1 原执行链中的嵌套调用

[ToolExecutor.execute_nested](../adam/tool_calls/executor.py) 仅在当前协调线程执行根 `execute_code` 动作期间可用；父动作退出后，保存下来的回调失效。每次底层调用仍由同一 ToolInvocation 执行参数、模式、路径、Hook、批准、附件及结果保存，不能在其他线程使用或再次递归编排。主计划等服务的当前来源在底层调用后恢复为父调用。

本轮的 `ToolBudget` 同时计算所有工具调用和编辑尝试。根编排入口占一次，每个底层调用再占一次；参数错误、隐藏工具拒绝等已派发尝试也计数。超过额度的下一项在开始记录及副作用前停止。后续模型批次使用累计值预检，不能因中间结果没有加入聊天而漏计。工具名单冻结于触发父调用的模型请求，编排内的发现操作不能立即开放新工具。

嵌套调用由宿主分配独立 ID，沿用父请求的 turn_id/request_id，并在工具开始与结束事件中保存 `parent_tool_call_id`。日志只接受同请求内尚未结束的根编排父项；不能跨请求挂接、改变父项或在子调用未结束时完成父项。Hook、修改检查点和动作记录仍引用各自真实底层调用 ID，沿日志关系追溯父项。

模型历史只保存父工具请求及聚合返回，原始底层结果留在 JSONL；因此缩减上下文没有丢弃执行证据。完整轮次提交仍校验全部根调用及其已经结束的嵌套记录，不能把子调用冒充模型独立发出的请求。失败输入保持诊断用途。

录制继续保存整批工具边界和实际会话增量，不新增嵌套模型请求。离线重放逐个导入 `tool_started`，由当前 ToolBudget 同样扣额；不构造执行器或启动 worker。新字段是会话 v4 中的可选来源扩展，旧的平面日志保持可读。

本阶段的 [业务测试](../tests/features/test_nested_tool_execution.py) 使用固定宿主编排回调和模拟 HTTP，实际经过原执行器、文件编辑、批准、Hook 契约、JSONL 与录制；[来源边界](../tests/test_nested_tool_log.py) 拒绝无效父项、提前完成、独立子消息和隐藏工具。共 24 项新场景通过；这些测试验证宿主执行链，不替代 B19.1/B14.1 的真实隔离证明，生产入口的验证见下文。

连同普通工具循环、并发、发现、重复失败、编辑修正、附件、任务压缩、运行状态及旧录制重放，175 项相关检查通过；Ruff、Pyright 通过。每次主线提交仍执行完整 pytest 门禁。

## B14.2.2 配置与模型使用

在用户 config.toml 中明确开启；该设置不自动安装 WSL 或 Bubblewrap，也不影响既有 run_command 的运行位置：

```toml
[orchestration]
enabled = true
distribution = "Ubuntu"
```

默认 enabled=false。distribution 只能是发行版名称，不接受路径、启动命令、挂载或宿主回退选项。每次实际编排都先探测该发行版；组装或查看工具目录不启动进程。配置示例见 [config.example.toml](../config.example.toml)。

按需模式下先发现 execute_code 及所需底层工具，下一次请求才能组合调用。模型传入 code 字符串，例如：

```python
info = call_tool("file_info", {"path": "notes.txt"})
content = call_tool("read_file", {"path": "notes.txt"})
result = {
    "size": info["size"],
    "matches": [row for row in content["lines"] if "TODO" in row["text"]],
}
```

Python 必须兼容隔离发行版的 3.8 版本。代码参数在 Hook 和后端探测前验证，不可编码、空或超长内容直接拒绝。当前只有同步串行 call_tool，单个脚本最多 16 次；同轮主额度也计算根入口与所有底层调用。plan 模式允许隔离计算，但每个底层动作仍检查自身模式；子任务的命令和直接编辑仍返回需要主任务批准，不因包装进脚本而放开。

结果中的 ok/status 说明脚本是否成功返回，result 中每项业务是否成功须检查各自事实。已知脚本非零退出保存 failed、exit_code 和 stderr；隔离不可用、启动竞争或协议失联以 isolation_error 停止轮次，不重试、不回退。取消和原日志故障保留原来的停止原因。

宿主另附 nested_calls，只包含已结束底层调用的 ID 和名称；worker 不能伪造这些身份。后续模型可以用这些 ID 为计划提供实际证据。若以整次 execute_code 作为“完成”证据，程序还要求其所有底层工具成功；命令必须退出 0，编辑必须 applied。脚本吞掉命令错误后正常返回，不能绕过原规则。发生失败后修好了某一步，可单独引用对应成功的底层 ID。需要 read_call_id 的提案也可在后续请求引用，而不要求 worker 自行猜测 ID。

### 验证边界

[生产组装测试](../tests/features/test_code_orchestration_loop.py) 使用模拟 HTTP 和隔离传输替身，实际经过原注册表、文件/命令、子任务、额度和日志；覆盖发现后调用、关闭、参数、后端/脚本/预算失败、子任务权限、配置和启动竞争，以及命令退出 0/7 的计划证据对照。替身不证明 OS 隔离。

[真实聊天验收入口](../scripts/chat_orchestration_acceptance.py) 则使用实际 WSL/Bubblewrap 和生产 build_application，模型仍为固定 HTTP 响应，付费请求为零。[已保存报告](evaluations/b14-chat.json) 验证两项真实文件工具、worker 筛选、总调用计数 3、父子来源、源正文未进入模型输入、JSONL 重启及删除源文件后的离线重放。

```powershell
python -m scripts.chat_orchestration_acceptance --distribution Ubuntu --output chat-rpc-result.json
```

报告必须新建。B14.3 的独立故障证据见下节，成功路径与失败清理分别验收。

B14.2.2 新增 17 项场景；与嵌套、隔离 API、子任务、录制、计划、配置和架构共 176 项通过。Ruff、Pyright 与文档链接已检查，主线提交仍执行全量 pytest。


## B14.3 真实故障与重启验收

[故障入口](../scripts/orchestration_fault_acceptance.py) 每例启动独立 Windows 宿主，经过生产聊天和真实 WSL/Bubblewrap。只在第一次真实底层工具结束后暂停，父进程确认 Linux supervisor、wrapper 和 worker 的身份后放行、取消或强杀宿主。代码带本例随机身份，清理核对 PID 和启动时间，不匹配其他测试或用户进程。

| 故障 | 观察到的行为 |
| --- | --- |
| 工作目录外读取 | 原路径权限拒绝，敏感哨兵不进入模型；模型可以继续说明拒绝事实 |
| 根入口与底层调用总计超额 | 第二次底层动作未开始，轮次以 tool_limit 停止；第一项已有记录保留 |
| worker 退出 7 | 工具保留非零退出事实；后续回答说明失败，不将退出改为成功 |
| worker 写出坏帧 | 以 isolation_error 停止，无后续模型请求或自动重跑 |
| 取消正在等待的 worker | 协作取消结束本轮，保留 interrupted 和原有效历史 |
| 直接强杀 Windows 宿主 | Linux 端收到通道断开并清理；重新持锁读取追加 restart_recovery，不重发、不提交失败输入 |

[真实报告](evaluations/b14-faults.json) 六例均确认所有已跟踪 Linux 进程退出，并两次打开会话验证恢复幂等。报告中的 succeeded 仅表示“说明失败的回答已完整保存”，不表示底层工具或用户任务成功。停止耗时是这次观察，不是所有机器和所有工具的延迟承诺。验收使用临时目录，模型为固定 MockTransport，付费请求 0；未测试的内核漏洞和真实模型编排效果不在结论内。

```powershell
python -m scripts.orchestration_fault_acceptance --distribution Ubuntu --output rpc-faults-result.json
```

[宿主管道测试](../tests/features/test_orchestration_cleanup.py) 新增 8 项：成功、EOF、残帧、坏帧、尾部杂字、启动器异常退出、取消、回调存储异常。它用真实 Python 子进程和管道替换 Linux 启动器，验证生产宿主清理和异常传播；不能代替上面的 OS 验收。与协议、隔离、嵌套、工具循环和架构共 112 项通过；Ruff、Pyright 和文档链接已检查，主线提交门禁仍运行全量 pytest。
