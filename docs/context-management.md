# 上下文管理

更新日期：2026-10-28。V3.1～V3.4 已实现并完成离线工程验收。见 [架构](architecture.md) 和 [练手 TODO](agent-practice-todo.md)。

## 已实现：预览与整轮选择

`/context` 查看下一次请求的基础上下文；`/context full` 展开消息和工具说明。不含尚未输入的新问题，不是最近实际请求快照；查询不创建会话、不写日志、不请求模型。

SessionStore 提供完整成功轮次，ContextBuilder 从最新往前取连续轮次，某轮放不下就停止，不越过它取更早的小轮次。系统提示、完整工具说明、当前用户输入及本轮工具链保留；受保护内容本身过大则明确停止。

原始 JSONL 全部保留，失败轮次只供诊断。被省略且未被摘要覆盖的旧要求不会被模型看到；V3.3 的摘要尽量保留重要信息，仍不保证无损。

实际 `request_started.context` 保存策略版本、保留/省略轮次 ID、原历史与最终输入估算。独立诊断报告会展示该信息；它记录选择依据，不重复保存完整 HTTP 请求。旧 v4 请求没有此字段仍可读取。

## E1 相关记忆

启用记忆后，每个新用户轮次按问题检索一次；完整条目和来源计入独立 memory 输入限额及总上下文预算。先为原有受保护输入预留空间，记忆不适合时可以不选入；选中快照在本轮工具循环中保持一致。

/context 不提供问题时不猜测记忆相关性；可使用 /context [full] --query 问题 预览一个假设请求。它不发模型请求或写聊天日志。实际使用的内容、版本和选择状态存入 request_started.context.memory，详见 [跨会话记忆](cross-session-memory.md)。

## 预算

在实际 `config.toml` 中配置，省略时采用默认值：

```toml
[context]
max_input_tokens = 32768
safety_margin_tokens = 2048
retain_recent_turns = 2
summary_max_output_tokens = 2048
prune_tool_results = true
auto_compact = true
compression_trigger_ratio = 0.8
compression_target_ratio = 0.6
tool_prune_threshold_chars = 4000
tool_prune_head_chars = 1500
tool_prune_tail_chars = 500
```

系统 + 选中历史 + 当前轮次 + 工具说明的估算，加安全余量，不能超过输入上限。输出由 `agent.max_output_tokens` 另计，默认 4096；输入上限加输出预留需适配所选模型，本版不查询模型真实窗口。

每条消息以紧凑 JSON 的 UTF-8 字节数除以 3 向上取整，再加 8；工具 Schema 整体另算一次。这是近似估算，不是模型 tokenizer，不能保证远端一定不超窗。`/usage` 使用服务端实际用量累计消费，与上下文估算分开。

每次工具结果加入后重新选择并检查。超限记录整轮失败及 `context_input_limit`，不发起本次请求、不新增未知用量。之前的请求、工具证据和已知消费保留。

## 手动摘要（已实现）

输入 `/compact`。默认保留最近 2 轮原文，不能因超限静默省略这些轮次。使用当前模型且不提供工具，最多 2048 输出 token；无会话或无旧素材时不调用模型。每次请求只处理能放入预算的最早连续一批，后续调用合并已有摘要并推进覆盖范围。

摘要按独立操作记录，`/context` 显示摘要 ID、覆盖轮数及估算，全文模式展开摘要。新程序继续读取没有摘要的 v4；旧程序不保证读取新增事件。

超时、无效响应、缺失用量、结算或摘要写入失败均停止；原历史与上个摘要保留。已知失败消费也计入会话；未知消费阻止续聊。崩溃恢复关闭遗留摘要操作，不推断结果，不重发。

## 旧工具结果缩减（已实现）

裁剪前估算输入（含余量）达到输入预算 80% 才处理。`prune_tool_results` 可关闭；`compression_trigger_ratio` 可调触发比例。默认正文超过 4000 字符才缩减，保留头 1500、尾 500 字符及来源标记；阈值和头尾长度均在 `[context]` 配置。

只处理近期保护范围外的成功轮次。保留 `ok/status/exit_code/error/stderr` 等状态与错误信息；如果保留这些信息后没有变短，则保留原结果。工具调用与结果 ID 不变，原始 JSONL 不改写。`request_started.context.pruned_tools` 记录来源、算法及长度，`/context` 显示缩减数。投影本身没有网络调用。

## 自动摘要（已实现）

实际发送前先缩减旧工具结果；缩减后、整轮选择前的完整候选输入加余量仍达到 80% 时，尝试一次摘要。先检查系统提示、当前轮次和受保护历史是否能放下；这些内容本身超限时直接停止，避免花费一次无法解决问题的摘要请求。

从最早未覆盖历史选择有界的连续整轮，与旧摘要合并，按约 60% 的目标规划素材。保护最近 2 轮，摘要输出预留按上限估算；整轮不可拆分、当前输入和实际摘要长度都会影响结果，60% 是规划目标，不保证达成。没有旧素材时不调用摘要；摘要素材本身放不下则明确报错。摘要后再选择历史并检查实际请求预算。

每个用户轮次最多尝试一次自动摘要，包括工具循环期间；失败停止、不自动重试。摘要占用该轮最多 8 次模型请求中的一次，开始前至少保留一次后续回答请求。摘要用量和聊天用量一起计入会话累计限额；摘要后重新检查消费，不能因压缩绕过限额。用量未知仍阻止后续消费。

摘要只有完整保存后才启用，并记录触发的 turn_id 及缩减素材来源。成功摘要可以在后续聊天失败时继续有效，因为它覆盖的是先前成功历史。原始记录保留，重启不重发。终端只显示简短的缩减或摘要提示，不增加逐次 token 输出。

`auto_compact = false` 关闭自动模型摘要；`prune_tool_results = false` 单独关闭旧工具结果缩减。两者相互独立，手动 `/compact` 仍可使用。`/context` 只执行纯构造与预览，不触发摘要；真实发送加上新输入后的上下文可能不同。

## 验收

V3.4b 的 `tests/features/test_auto_compaction.py` 覆盖自动触发、摘要素材缩减、请求/消费共用上限、当前工具结果超限、失败不重试、开关及不可缩减输入。2026-10-28 全量 **272 passed in 49.41s**，Ruff 与 Pyright 通过。

`tests/features/test_compaction.py` 覆盖命令、分批摘要、来源合并、成本、失败保留、配置保护和诊断。`test_process_crash.py` 新增摘要请求中、半行写入、提交后真实强杀及全新进程恢复，验证只有提交完成的摘要生效。

`tests/features/test_context_selection.py` 验证连续后缀、原日志保留、实际 HTTP 输入和诊断选择记录、重启一致及非法来源不落盘。`tests/test_context.py` 验证临界预算下整条工具链全留或全省略；V3.1 的预览、输入/工具说明超限和工具结果增长场景继续保留。

## 固定长对话离线对比

`tests/features/test_context_evaluation.py` 使用同一段“早期代号 ORION + 8 轮噪声”的历史，假模型仅根据可见材料返回代号或 unknown。可运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_context_evaluation.py -q -s
```

| 策略 | 回答可见代号 | 模型请求数 | 最后聊天请求的消息估算 token | 模拟累计用量 |
| --- | --- | --- | --- | --- |
| 全量历史（加大预算） | ORION | 1 | 8404 | 8424 |
| 整轮选择 | unknown | 1 | 7318 | 7338 |
| 自动摘要后选择 | ORION | 2 | 2176 | 8768 |

“消息估算”仅计算 messages，不含工具 Schema；模拟用量由假模型按消息估算和固定输出量生成，不是真实账单。该测试验证摘要素材含有早期约束、结果注入和记账流程。首次摘要使本次总消费更高，即使后续聊天输入变短；不能据此声称真实模型摘要无损或一定省钱。真实模型固定任务评估 V2.3 仍按用户决定跳过。

DSH 参考：[会话日志与模型上下文](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)、[摘要](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)、[工具结果缩减](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)。Adam 的整轮选择是练手简化，未复刻 DSH 插件机制。
