# V1.3.1：会话执行轨迹

更新日期：2026-10-28

这一版把“用户输入 → 模型请求 → 工具处理”关联起来。即使最后一次模型请求失败，已经落盘的输入、请求与工具结果仍能查询 JSONL；它们不自动成为下一次模型请求的有效聊天历史。

完整的请求/轮次状态、结束原因、耗时、诊断命令及重启时追加中断记录，属于后续 V1.3.2 / V1.3.3。本版不能仅凭用量未知解释具体失败原因。

## 关联关系

```text
session_id：一个会话，对应一个 JSONL 文件
  └─ turn_id：一次用户输入及其后续处理
       ├─ request_id：第一次模型请求
       │    ├─ tool_call_id：模型要求的一个工具调用
       │    └─ tool_call_id：同次响应中的另一个调用
       └─ request_id：收到工具结果后的下一次模型请求
```

一个轮次可以有多次请求，一次请求可以产生多个工具调用。会话 ID 为 8 位十六进制；轮次、请求和命令执行 ID 由程序生成，工具调用 ID 来自模型。不同轮次允许模型复用工具调用 ID，因此查看工具时要同时看 turn_id。

所有记录采用 `type`、UTC `timestamp`、`payload`。`session_meta.payload.version` 为 3。

| 事件 | payload 的关键字段 | 说明 |
| --- | --- | --- |
| turn_started | turn_id、user_text | 用户提交时保存输入，尚未成为成功聊天历史 |
| request_started | turn_id、request_id、model | 调用模型前记录本次请求 |
| request_usage | turn_id、request_id、input_tokens、output_tokens | 结算可用用量；未知时保留 null，不算成零 |
| tool_started | turn_id、request_id、tool_call_id、name、arguments | 即将进入工具处理；arguments 为模型给出的原始 JSON 字符串 |
| tool_finished | turn_id、request_id、tool_call_id、output | 已取得返回值；output 为实际回传模型的有界 JSON 字符串 |
| command_started / command_finished / command_denied | 上述三个关联 ID、execution_id 及命令细节 | run_command 的审批和进程执行记录 |
| message | turn_id、message | 最终成功时追加的完整协议消息，包含工具请求和结果 |
| turn_completed | turn_id | 完整成功消息块的提交标记 |

三个容易混淆的区别：

- request_usage 说明用量已经结算，不证明响应有效或整个轮次成功。
- tool_finished 说明工具返回了结果；文件不存在、参数错误和用户拒绝也能返回结果，业务是否成功应读取 output。
- tool_started 说明进入工具处理，可能随后在参数校验或用户确认处结束；它不证明命令进程已经启动。命令的实际执行边界由 command_* 描述。

## 失败后留下什么

以“读取一个文件，然后第二次模型请求返回 HTTP 503”为例。下面用 T1、R1 等缩写表示关联 ID，展示事件顺序，并非可直接写入文件的完整 JSONL：

```text
turn_started     T1       user_text="读取 sample.txt 并解释"
request_started  T1 R1    model=...
request_usage    T1 R1    已知用量
 tool_started    T1 R1 C1 name=read_file, arguments=...
 tool_finished   T1 R1 C1 output=文件内容和定位信息
request_started  T1 R2    model=...
request_usage    T1 R2    用量未知
```

从记录能确认 T1 的输入、R1/R2 同属一轮、C1 来自 R1、读取结果是什么。没有 `turn_completed`，因此这轮输入和工具结果不会进入 `load_messages()` 的有效历史。

这个例子中的 503 是测试预设的故障；**本版日志尚未保存 HTTP 错误类别，单看未知用量不能断言就是 503。** 后续状态与结束原因练习正是为了补齐这个缺口。

若最后回答成功，程序会追加完整 message 消息块和 turn_completed，仍使用 T1；不会在保存时另造一个轮次 ID。

## 写入顺序与失败边界

1. 保存输入后才进入模型请求流程，记录请求开始后才调用模型。
2. tool_started 保存成功后才执行工具，tool_finished 保存成功后才把结果交回模型。
3. command_* 继续独立记录确认和进程边界；后续模型失败不会删除命令证据。
4. 写入前验证轮次、请求与调用关联；非法引用、重复结束、跨轮结算或成功消息与原始输入/执行结果不一致时拒绝追加。
5. 未完成的诊断轮次或工具记录允许读取。缺失用量仍保持未知，只有开始事件的命令仍标为 unknown；相应的禁止继续调用规则保持不变。
6. 尾部半行、非法事件或残缺成功消息块仍严格报错，不自动截断修复，也不自动追加 interrupted。已有的可捕获写入失败仍尝试回滚本次追加。

当工具结果写入失败时，真实工具可能已经执行；此时停止本轮，不把失败包装成可让模型重试的普通工具结果。

## 代码阅读顺序

- `adam/conversation.py`：开始轮次，关联模型请求，成功后提交完整历史。
- `adam/tool_calls/executor.py`：统一围绕所有工具记录开始和结果，衔接命令细节记录。
- `adam/storage.py`：公开存储接口、文件锁和追加落盘。
- `adam/session_log.py`：解析事件并验证关联，区分诊断记录与已提交历史。
- `adam/protocol.py`：校验工具消息协议和完整成功轮次。

## 验收

定向测试文件为 `tests/features/test_execution_trace.py`，不调用真实付费模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_trace.py -v
```

检查以下结果：

- 第一次请求返回 read_file，第二次失败，原始输入、两次请求和读取结果仍可从日志关联。
- run_command 的通用工具记录与命令事件指向同一轮次、请求和调用。
- 成功链沿用输入时的 turn_id；失败输入不会混入后续有效上下文。
- 非法关联被拒绝后，原日志仍可正常读取。
- 输入记录失败不请求模型；工具开始记录失败不执行工具；工具结束记录失败不继续请求模型。

本轮自动化验收结果记录在 [练手 TODO](agent-practice-todo.md)。独立进程强制终止与重启状态处理还没有完成，不能用上述故障注入替代。

## 旧会话

v3 明确拒绝旧 v1/v2，不自动迁移或删除。旧文件留在当前会话目录会使 `/sessions` 报错；可自行将旧文件归档到会话目录之外，或者把 `config.toml` 中的 `storage.sessions_dir` 改为新的目录，再新建会话。程序不会代替用户移动旧数据。
