# 上下文管理

更新日期：2026-10-28。V3.1/V3.2/V3.3 已实现；V3.4 方案已确认，按小步验收并提交。见 [架构](architecture.md) 和 [练手 TODO](agent-practice-todo.md)。

## 已实现：预览与整轮选择

`/context` 查看下一次请求的基础上下文；`/context full` 展开消息和工具说明。不含尚未输入的新问题，不是最近实际请求快照；查询不创建会话、不写日志、不请求模型。

SessionStore 提供完整成功轮次，ContextBuilder 从最新往前取连续轮次，某轮放不下就停止，不越过它取更早的小轮次。系统提示、完整工具说明、当前用户输入及本轮工具链保留；受保护内容本身过大则明确停止。

原始 JSONL 全部保留，失败轮次只供诊断。被省略且未被摘要覆盖的旧要求不会被模型看到；V3.3 的摘要尽量保留重要信息，仍不保证无损。

实际 `request_started.context` 保存策略版本、保留/省略轮次 ID、原历史与最终输入估算。独立诊断报告会展示该信息；它记录选择依据，不重复保存完整 HTTP 请求。旧 v4 请求没有此字段仍可读取。

## 预算

在实际 `config.toml` 中配置，省略时采用默认值：

```toml
[context]
max_input_tokens = 32768
safety_margin_tokens = 2048
retain_recent_turns = 2
summary_max_output_tokens = 2048
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

## 后续自动摘要方案

- 每次最多一次摘要请求；从最早未覆盖历史按整轮选择能放下的一批，与旧摘要合并。无旧历史不请求；不可拆分素材过大则报错。
- 摘要独立记录开始、请求、消费、结果和结束；完整保存后才启用。原始历史不改写，失败保留上一份摘要，重启不自动重发。
- V3.4：默认自动压缩，裁剪前估算占用达 80% 触发，目标约 60%。先缩旧工具结果，再必要时摘要，最后整轮选择；每轮最多一次自动摘要。
- 工具结果超过 4000 字符才缩减，保留头尾约 2000 字符和定位标记；保护近期 2 轮与当前轮次，保留错误/退出码等信息。
- 所有摘要计入会话消费；自动摘要计入本轮模型请求上限。用量未知继续沿用阻止消费规则；失败不自动重试。
- 每步运行离线验收，不调用付费 API。长对话工程对比不能证明真实模型摘要无损。

## 验收

`tests/features/test_compaction.py` 覆盖命令、分批摘要、来源合并、成本、失败保留、配置保护和诊断。`test_process_crash.py` 新增摘要请求中、半行写入、提交后真实强杀及全新进程恢复，验证只有提交完成的摘要生效。

`tests/features/test_context_selection.py` 验证连续后缀、原日志保留、实际 HTTP 输入和诊断选择记录、重启一致及非法来源不落盘。`tests/test_context.py` 验证临界预算下整条工具链全留或全省略；V3.1 的预览、输入/工具说明超限和工具结果增长场景继续保留。

DSH 参考：[会话日志与模型上下文](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)、[摘要](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)、[工具结果缩减](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)。Adam 的整轮选择是练手简化，未复刻 DSH 插件机制。
