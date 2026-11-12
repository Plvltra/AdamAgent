# V1.3：会话执行轨迹与状态

更新日期：2026-11-12

这一版把“用户输入 → 模型请求 → 工具处理”关联起来。即使最后一次模型请求失败，已经落盘的输入、请求与工具结果仍能查询 JSONL；它们不自动成为下一次模型请求的有效聊天历史。

本次完成工单01：请求与轮次在原有状态之外保存结束原因、耗时和受控错误详情。独立开发诊断入口、重启恢复和真实进程强杀验收仍未实现，按新工单顺序继续。不新增 `/turn`、`/turns` 或 `/diagnose` 聊天命令。

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

所有记录采用 `type`、UTC `timestamp`、`payload`。`session_meta.payload.version` 为 5。

| 事件 | payload 的关键字段 | 说明 |
| --- | --- | --- |
| turn_started | turn_id、user_text | 用户提交时保存输入，尚未成为成功聊天历史 |
| request_started | turn_id、request_id、model | 调用模型前记录本次请求 |
| request_usage | turn_id、request_id、input_tokens、output_tokens | 结算可用用量；未知时保留 null，不算成零 |
| request_finished | turn_id、request_id、status、结束字段 | 请求终态；与 request_usage 同一追加块保存 |
| turn_finished | turn_id、status、结束字段 | 失败或中断的轮次终态，不提交消息 |
| tool_started | turn_id、request_id、tool_call_id、name、arguments | 即将进入工具处理；arguments 为模型给出的原始 JSON 字符串 |
| tool_finished | turn_id、request_id、tool_call_id、output | 已取得返回值；output 为实际回传模型的有界 JSON 字符串 |
| command_started / command_finished / command_denied | 上述三个关联 ID、execution_id 及命令细节 | run_command 的审批和进程执行记录 |
| message | turn_id、message | 最终成功时追加的完整协议消息，包含工具请求和结果 |
| turn_completed | turn_id、结束字段 | 完整成功消息块的提交标记 |

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
request_finished T1 R1    status=succeeded
 tool_started    T1 R1 C1 name=read_file, arguments=...
 tool_finished   T1 R1 C1 output=文件内容和定位信息
request_started  T1 R2    model=...
request_usage    T1 R2    用量未知
request_finished T1 R2    status=failed
turn_finished   T1       status=failed
```

从记录能确认 T1 的输入、R1/R2 同属一轮、C1 来自 R1、读取结果是什么。没有 `turn_completed`，因此这轮输入和工具结果不会进入 `load_messages()` 的有效历史。

这个例子中的 503 是测试预设的故障。v5 的第二次 request_finished 和 turn_finished 都保存 reason=api_http_error、http_status=503；失败原因不从 failed 状态或未知用量猜测。

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
- `adam/persistence/store.py`：公开存储接口、文件锁和追加落盘。
- `adam/persistence/session_log.py`：解析事件并验证关联，区分诊断记录与已提交历史。
- `adam/protocol.py`：校验工具消息协议和完整成功轮次。

## 验收

定向测试文件包括 `tests/features/test_execution_outcomes.py`、`tests/features/test_execution_trace.py` 和 `tests/features/test_execution_states.py`，不调用真实付费模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_outcomes.py tests/features/test_execution_trace.py tests/features/test_execution_states.py tests/test_storage.py -v
```

检查以下结果：

- 第一次请求返回 read_file，第二次失败，原始输入、两次请求和读取结果仍可从日志关联。
- run_command 的通用工具记录与命令事件指向同一轮次、请求和调用。
- 成功链沿用输入时的 turn_id；失败输入不会混入后续有效上下文。
- 有效请求成功与整轮成功分开，未知用量仍保持未知；请求/工具阶段中断与程序错误有不同终态。
- 保存失败只结束轮次，不抹去请求成功；失败日志再次写入失败不掩盖原始异常。
- 非法关联被拒绝后，原日志仍可正常读取。
- 输入记录失败不请求模型；工具开始记录失败不执行工具；工具结束记录失败不继续请求模型。

本轮自动化验收结果记录在 [练手 TODO](agent-practice-todo.md)。独立进程强制终止与重启状态处理还没有完成，不能用上述故障注入替代。

## 结束字段与计时口径（工单01）

三类结束事件均必填以下字段；成功的 turn_completed 仍不额外写 status，由事件类型表示成功。

| 字段 | 含义与校验 |
| --- | --- |
| reason | EndReason 定义的稳定字符串，不通过中文提示推断 |
| duration_ms | 非负整数毫秒；未计时的直接存储调用为 null，不能用0伪装未知；Conversation运行路径实际计时 |
| error_message | 成功为 null；失败/中断为受控说明，不复制服务端正文、原始意外异常或 traceback |
| http_status | 仅 api_http_error 时保存实际非2xx HTTP状态码；其他原因必须为 null |

正常结束为 completed。API 原因为 api_timeout、api_connection、api_http_error、invalid_response；预算为 request_limit、tool_limit、session_limit、usage_unknown。用户中断为 interrupted，SystemExit 为 process_exit，未知程序异常为 internal_error。已有业务错误另有 storage_error、tool_error、command_log_error、answer_save_failed 等分类。请求正常返回但用量缺失时，请求仍为 completed，轮次为 usage_unknown。

- 请求：start_request 写入完成、工具说明准备好后开始，模型调用和响应解析结束时停止；不包含开始或结算日志写入。
- 轮次：start_turn 写入完成后开始，到即将最终收尾前停止；包含中间工具、模型和日志耗时，不含最终成功提交或失败记录写入。若成功提交尝试失败，随后记录失败的耗时包含这次失败尝试。
- 使用 monotonic_ns 的差值向下取整为毫秒；timestamp 仍是每条记录生成时的 UTC 时间，不用于计算持续时间。
- outcome.py 只持有、分类和校验数据；Conversation 测量时间，SessionStore 落盘，SessionLog 严格校验并重建字段。缺失新字段、负耗时、布尔耗时、状态/原因矛盾均拒绝读取。

## 旧会话

v5 明确拒绝旧 v1～v4，不做兼容或迁移。2026-11-12 已按用户授权删除 `C:\Users\zhangzhengxun\.adam\sessions` 下8个旧JSONL（v1/v2/v4），删除前核对路径、版本并取得锁，配置和记忆保留。程序运行时不会自动删除旧文件。以后遇到旧版或未知版会话会提示新建，不能静默按新格式解释。

## 离线验收产生的 JSONL 摘录

以下三行取自本轮 `test_two_requests_keep_separate_outcomes[True]` 实际生成的日志，不是手写预期数据。测试使用模拟 HTTP、真实文件读取和可控时钟：请求125ms、工具25ms、请求250ms，轮次400ms；这些数字不是实际网络性能。为便于阅读，仅摘录结束事件，省略元信息、开始、工具和用量事件，因此不能把此片段当作完整会话导入。

```jsonl
{"type": "request_finished", "timestamp": "2026-11-12T06:34:01.991519+00:00", "payload": {"turn_id": "8b90aebc4f023f7ebfc4f00d1ffa5a65", "request_id": "55238f78fbd58bfa3afd472e006aa282", "status": "succeeded", "reason": "completed", "duration_ms": 125, "error_message": null, "http_status": null}}
{"type": "request_finished", "timestamp": "2026-11-12T06:34:02.061985+00:00", "payload": {"turn_id": "8b90aebc4f023f7ebfc4f00d1ffa5a65", "request_id": "966a31dc340a2d10dc1fe0472cfcae1c", "status": "failed", "reason": "api_http_error", "duration_ms": 250, "error_message": "模型请求错误：HTTP 503：请求失败；用量未知，未自动重试。", "http_status": 503}}
{"type": "turn_finished", "timestamp": "2026-11-12T06:34:02.074533+00:00", "payload": {"turn_id": "8b90aebc4f023f7ebfc4f00d1ffa5a65", "status": "failed", "reason": "api_http_error", "duration_ms": 400, "error_message": "模型请求错误：HTTP 503：请求失败；用量未知，未自动重试。", "http_status": 503}}
```
