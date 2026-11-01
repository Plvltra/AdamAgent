# B04 模型可见状态

当前完成 B04.1：只读数据契约与真实计数来源。B04.2 的模型输入注入、记录和重放尚未接入，因此当前正常聊天不会新增状态消息或终端输出。

## 数据从哪里来

| 状态 | 口径与来源 |
| --- | --- |
| requests | RequestPolicy.snapshot：本轮物理尝试，明确未发出的连接重试也占次数；未开始的下一次仍在 remaining 内 |
| tools | 宿主统计的本轮全部工具次数，不能用只统计文件修改的 ToolBudget 替代 |
| tokens | SharedTokenBudget.snapshot：锁内同时读取整组已结算消费、未知标记和在途估算预留；不跨线程读取会话文件 |
| failures | NoProgressGuard 产生的连续相同失败计数与 SHA-256 指纹；未观察为 null，0 不意味着任务成功 |
| environment | 宿主已验证的绝对工作目录和 ExecutionMode；没有 API 密钥、环境变量或新授权字段 |
| pending_agents | 待收集子任务 ID；null 是未知，[] 是确认为无，出现 ID 不表示其任务成功 |

这些值通过 [RuntimeState](../adam/contracts/runtime.py) 汇总。它是不可变值对象，as_record 只复制 JSON 数据，格式为 runtime_state_v1；本小步不提供外部记录解码，也不把模型提供的字典转成可信状态。

## 剩余额度不等于执行许可

请求和工具 remaining = max(0, maximum − used)，实际 used 超额时仍完整保留。token remaining = max(0, maximum − known_used − reserved)，但仅在消费和预留均已知时计算；否则为 null。known_used 保留已知小计，不因其他消费未知而清零。

例如总额度 1000、已结算 150、两个在途请求分别预留 200 和 300，快照显示余量 350。读取快照本身不预留这 350，其他子任务可能立即占用它；真正请求时仍必须通过共享账本的原子 reserve。预留是本地估算，也不能保证最终服务端消费不超过上限。

整个 RuntimeState 汇总的各来源并非全局原子快照；只有共享 token 的消费和预留在同一锁内读取。B04.2 注入时须标明输入构造时刻的含义，不能声称连接重试复用的输入是实时状态。

## 验收与下一步

7 项 [业务 API 场景](../tests/features/test_runtime_state.py) 验证真实重试计数、嵌套预留/释放、未知消费、旧快照无法越过执行检查、连续失败重置和不可变汇总。11 项 [数据边界](../tests/test_runtime_state.py) 拒绝负值、布尔假计数、错误模式、相对目录和重复子任务 ID；连同架构检查共 39 项通过。

下一步 B04.2 接入动态上下文、提供有界文本并记录实际发送值；B04.3 进行主/子任务与失败、额度组合验收。CLI 仍按需查询用量，不恢复每次请求都打印 token，也不新增 /turn 或 /diagnose。

相关说明：[完整练习路线](agent-book-practice-plan.md)、[上下文排列](cache-observation.md)、[当前架构](architecture.md)。
