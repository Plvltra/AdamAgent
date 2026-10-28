# V1.3：会话执行轨迹与状态

更新日期：2026-10-28

这一版把“用户输入 → 模型请求 → 工具处理”关联起来。即使最后一次模型请求失败，已经落盘的输入、请求与工具结果仍能查询 JSONL；它们不自动成为下一次模型请求的有效聊天历史。

已完成用户清单第 2、3 项：分别记录请求和轮次状态，以及结束原因、受控错误说明和耗时。独立开发诊断入口（第 4 项）、重启恢复（第 5 项）和真实进程强杀验收（第 6 项）仍未实现，按最新授权，每项验收后单独提交并继续下一项。不新增 `/turn`、`/turns` 或 `/diagnose` 聊天命令。

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

所有记录采用 `type`、UTC `timestamp`、`payload`。`session_meta.payload.version` 为 4。

| 事件 | payload 的关键字段 | 说明 |
| --- | --- | --- |
| turn_started | turn_id、user_text | 用户提交时保存输入，尚未成为成功聊天历史 |
| request_started | turn_id、request_id、model | 调用模型前记录本次请求 |
| request_usage | turn_id、request_id、input_tokens、output_tokens | 结算可用用量；未知时保留 null，不算成零 |
| request_finished | turn_id、request_id、status、reason、duration_ms、error | 请求终态；与 request_usage 同一追加块保存 |
| turn_finished | turn_id、status、reason、duration_ms、error | 失败或中断的轮次终态，不提交消息 |
| tool_started | turn_id、request_id、tool_call_id、name、arguments | 即将进入工具处理；arguments 为模型给出的原始 JSON 字符串 |
| tool_finished | turn_id、request_id、tool_call_id、output | 已取得返回值；output 为实际回传模型的有界 JSON 字符串 |
| command_started / command_finished / command_denied | 上述三个关联 ID、execution_id 及命令细节 | run_command 的审批和进程执行记录 |
| message | turn_id、message | 最终成功时追加的完整协议消息，包含工具请求和结果 |
| turn_completed | turn_id、status、reason、duration_ms、error | 完整成功消息块的提交标记 |

几个容易混淆的区别：

- request_usage 说明用量已经结算，不证明响应有效或整个轮次成功。
- request_finished 的 succeeded 表示有效模型响应；即使响应未报告用量，请求也可成功，但整轮仍按预算规则停止。
- turn_completed 只有在完整消息块通过校验并提交后才产生，表示轮次 succeeded；不能单独调用 finish_turn 声称成功。
- tool_finished 说明工具返回了结果；文件不存在、参数错误和用户拒绝也能返回结果，业务是否成功应读取 output。
- tool_started 说明进入工具处理，可能随后在参数校验或用户确认处结束；它不证明命令进程已经启动。命令的实际执行边界由 command_* 描述。

## 失败后留下什么

以“读取一个文件，然后第二次模型请求返回 HTTP 503”为例。下面用 T1、R1 等缩写表示关联 ID，展示事件顺序，并非可直接写入文件的完整 JSONL：

```text
turn_started     T1       user_text="读取 sample.txt 并解释"
request_started  T1 R1    model=...
request_usage    T1 R1    已知用量
request_finished T1 R1    status=succeeded, reason=completed, duration_ms=125
 tool_started    T1 R1 C1 name=read_file, arguments=...
 tool_finished   T1 R1 C1 output=文件内容和定位信息
request_started  T1 R2    model=...
request_usage    T1 R2    用量未知
request_finished T1 R2    status=failed, reason=api_http, duration_ms=250, error.http_status=503
turn_finished   T1       status=failed, reason=api_http, duration_ms=400, error.http_status=503
```

从记录能确认 T1 的输入、R1/R2 同属一轮、C1 来自 R1、读取结果是什么。没有 `turn_completed`，因此这轮输入和工具结果不会进入 `load_messages()` 的有效历史。

这个例子的假 HTTP 返回 503，测试中的单调时钟分别推进 125、250 毫秒，工具阶段推进 25 毫秒。日志能直接区分请求耗时与整轮 400 毫秒，并查到 api_http 和 HTTP 503；不会保存服务端错误正文。用量未知仍是独立事实，不能凭未知用量猜测失败原因。

若最后回答成功，程序会追加完整 message 消息块和 turn_completed，仍使用 T1；不会在保存时另造一个轮次 ID。

## 状态规则（第 2 项）

`turn_started` / `request_started` 将对应投影设为 `in_progress`。请求由 request_finished 转为 `succeeded`、`failed` 或 `interrupted`；轮次由 turn_completed 转为 succeeded，或由 turn_finished 转为 failed / interrupted。终态不可重复结束或逆转，已结束轮次不能发起新请求。

| 场景 | 请求状态 | 轮次状态 | 用量 |
| --- | --- | --- | --- |
| 有效回答且消息提交成功 | succeeded | succeeded | 服务端报告值 |
| 请求返回工具调用，之后预算不足 | succeeded | failed | 已知值保留 |
| 第二次请求发生 API/协议错误 | 第一请求 succeeded、第二请求 failed | failed | 分别结算，未知不算零 |
| 有效响应缺少用量 | succeeded | failed | 未知，禁止继续消费 |
| 回答生成后消息提交失败 | succeeded | failed（若收尾写入成功） | 已结算值保留 |
| 请求中捕获 KeyboardInterrupt / SystemExit | interrupted | interrupted | 未知 |
| 请求成功后在工具阶段捕获中断 | succeeded | interrupted | 已结算值保留 |
| 意外程序异常 | 异常发生在请求内时 failed | failed | 有证据才记已知值 |

Conversation.send 负责轮次异常收尾，_run_turn 保留模型与工具循环。请求边界单独结算用量与终态；原始异常继续传播，CLI 的公共错误展示无需增加异常分支。

若失败状态也无法落盘，程序保留最初的异常并附加受控说明，日志只反映最后成功写入的事实，可能仍为 in_progress。当前重开仅校验，不自动补 interrupted；强制终止也无法执行异常收尾。这些属于第 5、6 项，不能因本项测试通过提前宣称完成。

## 结束原因与耗时（第 3 项）

三个终态事件都采用相同字段，由 `ExecutionOutcome` 生成并由日志回放再次校验：

| 字段 | 含义 |
| --- | --- |
| status | succeeded、failed 或 interrupted |
| reason | 稳定原因码，查询不依赖中文提示文本 |
| duration_ms | 单调时钟测量的整毫秒；无法可靠测量为 null，不能把未知当作 0 |
| error | 成功为 null；失败为受控 message 和可选数值 http_status |

| 原因码 | 场景 |
| --- | --- |
| completed | 操作正常完成；轮次还须完整消息提交成功 |
| api_timeout / api_connection / api_http | 请求超时、网络连接失败、非 2xx HTTP 响应 |
| invalid_response | 模型消息、协议或结束标记无效 |
| request_limit / tool_limit / session_token_limit | 达到本轮模型次数、工具次数或会话 token 限额 |
| usage_unknown | 用量不可靠，按预算策略停止；请求本身可能已成功 |
| answer_save_failed / request_save_failed | 最终消息提交失败 / 请求用量与终态结算失败 |
| storage_error / command_log_error / execution_unknown | 存储故障、命令日志故障、已有命令结果未知 |
| interrupted / process_exit | 捕获到用户中断 / SystemExit |
| internal_error | 未预期的程序异常；只记通用说明，原异常继续传播 |

`agent_error`、`api_error`、`budget_exhausted` 是公共错误的兜底类别，具体业务边界优先提供表中的精确原因。错误说明只取公共契约中的受控文本，不复制原始系统异常、堆栈、服务端错误正文或未保存回答。

计时边界：

- 请求：request_started 写入完成后开始，到模型调用及响应解析完成或抛错为止；不含开始与结算日志的写入。
- 轮次：turn_started 写入完成后开始，到终态生成为止，包含工具执行、用户确认等待和中间落盘；成功时不含最终消息块自身的写入。
- 使用进程内单调时钟，不用两个 UTC 时间戳相减。UTC timestamp 仍用于定位事件发生时间。
- 强杀或终态写入失败时，没有可靠的结束记录；不补一个虚假的零耗时。跨进程恢复不能复用此前单调时钟起点，这部分留在第 5 项。

仍使用 v4 格式。早期 v4 缺少整组详情时可以读取，原因与耗时保持 null；只缺部分字段、负数/布尔耗时、成功携带错误等非法组合拒绝读取。旧 v1/v2/v3 规则不变。

## 写入顺序与失败边界

1. 保存输入后才进入模型请求流程，记录请求开始后才调用模型。
2. tool_started 保存成功后才执行工具，tool_finished 保存成功后才把结果交回模型。
3. command_* 继续独立记录确认和进程边界；后续模型失败不会删除命令证据。
4. 写入前验证轮次、请求与调用关联；非法引用、重复结束、跨轮结算或成功消息与原始输入/执行结果不一致时拒绝追加。
5. 未完成的诊断轮次或工具记录允许读取。缺失用量仍保持未知，只有开始事件的命令仍标为 unknown；相应的禁止继续调用规则保持不变。
6. 尾部半行、非法事件或残缺成功消息块仍严格报错，不自动截断修复，也不自动追加 interrupted。已有的可捕获写入失败仍尝试回滚本次追加。

当工具结果写入失败时，真实工具可能已经执行；此时停止本轮，不把失败包装成可让模型重试的普通工具结果。

## 代码阅读顺序

- `adam/conversation.py`：开始轮次，关联模型请求，分别计时并记录结果，成功后提交完整历史。
- `adam/errors.py`、`adam/outcome.py`：稳定失败类别与共用终态字段，生成受控说明并校验状态组合。
- `adam/tool_calls/executor.py`：统一围绕所有工具记录开始和结果，衔接命令细节记录。
- `adam/storage.py`：公开存储接口、文件锁和追加落盘。
- `adam/session_log.py`：解析事件并验证关联，区分诊断记录与已提交历史。
- `adam/protocol.py`：校验工具消息协议和完整成功轮次。

## 验收

定向测试覆盖轨迹、状态与结果详情，不调用真实付费模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_trace.py tests/features/test_execution_states.py tests/features/test_execution_outcomes.py -v
```

检查以下结果：

- 第一次请求返回 read_file，第二次失败，原始输入、两次请求和读取结果仍可从日志关联。
- run_command 的通用工具记录与命令事件指向同一轮次、请求和调用。
- 成功链沿用输入时的 turn_id；失败输入不会混入后续有效上下文。
- 有效请求成功与整轮成功分开，未知用量仍保持未知；请求/工具阶段中断与程序错误有不同终态。
- 保存失败只结束轮次，不抹去请求成功；失败日志再次写入失败不掩盖原始异常。
- 通过固定时钟验证两次请求与整轮的精确毫秒数，分别覆盖成功和第二次请求 HTTP 503。
- 超时、连接失败、协议错误、中断、退出、程序错误有独立原因，原始错误内容不进入日志。
- 复用额度和保存失败场景核对原因；基础测试拒绝非法详情，早期 v4 的未知字段不伪造。
- 非法关联被拒绝后，原日志仍可正常读取。
- 输入记录失败不请求模型；工具开始记录失败不执行工具；工具结束记录失败不继续请求模型。

本轮自动化验收结果记录在 [练手 TODO](agent-practice-todo.md)。独立进程强制终止与重启状态处理还没有完成，不能用上述故障注入替代。

## 旧会话

v4 明确拒绝旧 v1/v2/v3，不自动迁移或删除。旧文件留在当前会话目录会使 `/sessions` 报错；可自行将旧文件归档到会话目录之外，或者把 `config.toml` 中的 `storage.sessions_dir` 改为新的目录，再新建会话。程序不会代替用户移动旧数据。
