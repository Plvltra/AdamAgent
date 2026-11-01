# V1.3：会话执行轨迹与状态

更新日期：2026-11-01

这一版把“用户输入 → 模型请求 → 工具处理”关联起来。即使最后一次模型请求失败，已经落盘的输入、请求与工具结果仍能查询 JSONL；它们不自动成为下一次模型请求的有效聊天历史。

已完成用户清单第 2、3 项：分别记录请求和轮次状态，以及结束原因、受控错误说明和耗时。独立开发诊断入口（第 4 项）已完成；重启恢复（第 5 项）已完成，真实进程强杀验收（第 6 项）已完成，各项分别验收与提交。不新增 `/turn`、`/turns` 或 `/diagnose` 聊天命令。

## 关联关系

```text
session_id：一个会话，对应一个 JSONL 文件
  └─ turn_id：一次用户输入及其后续处理
       ├─ request_id：第一次模型请求
       │    ├─ tool_call_id：模型要求的一个工具调用
       │    └─ tool_call_id：同次响应中的另一个调用
       └─ request_id：收到工具结果后的下一次模型请求
```

一个轮次可以有多次请求，一次请求可以产生多个工具调用。会话 ID 为 8 位十六进制；轮次、请求和命令执行 ID 由程序生成，根工具调用 ID 来自模型。B14.2.1 的嵌套工具 ID 由宿主生成，并以 parent_tool_call_id 关联同请求的根 execute_code；不伪造额外模型请求。不同轮次允许模型复用工具调用 ID，因此查看工具时要同时看 turn_id。

所有记录采用 `type`、UTC `timestamp`、`payload`。`session_meta.payload.version` 为 4。

| 事件 | payload 的关键字段 | 说明 |
| --- | --- | --- |
| turn_started | turn_id、user_text；C00.3 可选 media 完整引用列表 | 用户提交时保存输入，尚未成为成功聊天历史；媒体须有当前会话先前已提交来源，成功 user 消息/当前摘要问题不得替换或遗漏，见 [多模态输入](multimodal-input.md) |
| request_started | turn_id、request_id、model、context | 调用模型前记录本次请求及上下文选择依据 |
| request_usage | turn_id、request_id、input_tokens、output_tokens；可选 cache_usage、input_prefix | 总用量独立结算；未知保持 null，缓存与本地输入前缀仅作诊断，见 [缓存观测](cache-observation.md) |
| request_finished | turn_id、request_id、status、reason、duration_ms、error | 请求终态；与 request_usage 同一追加块保存 |
| turn_finished | turn_id、status、reason、duration_ms、error | 失败或中断的轮次终态，不提交消息 |
| tool_started | turn_id、request_id、tool_call_id、name、arguments；嵌套时加 parent_tool_call_id | 即将进入工具处理；arguments 为模型或 worker 提交的 JSON 参数 |
| tool_finished | turn_id、request_id、tool_call_id、output；嵌套时保留同一 parent_tool_call_id | 已取得返回值；output 为实际回传模型或 worker 的有界 JSON 字符串 |
| hook_started / hook_finished | 父轮次/请求/工具、hook_id、event、execution_mode、result | T5 工具前后检查；独立诊断按轮次输出 hooks，结果与工具输出独立，详见 [Hooks 契约](lifecycle-hooks.md) |
| command_started / command_finished / command_denied | 上述三个关联 ID、execution_id 及命令细节 | run_command / run_isolated_command 的批准和进程执行记录；隔离入口另存环境与输入来源版本 |
| edit_planned / edit_started / edit_finished | 父轮次/请求/工具、edit_id、路径、版本与状态 | 计划、批准后写入边界及实际结果，详情见下文 |
| undo_requested / undo_prepared / undo_started / undo_finished | undo_id、原 edit_id、逆向版本、source_* 来源及结果 | T7.2 用户独立撤销，顶层诊断输出 undos，不伪造模型轮次 |
| proposal_submitted | proposal_id、owner、子轮次/请求/提交调用、read、edit、target_version | T11.1 不可变候选；核对实际读取和提交参数，不代表批准或应用，见 [修改提案](edit-proposals.md) |
| plan_updated | turn_id、request_id、tool_call_id、version、steps、reason、replan_count、max_steps、max_replans | E3 完整计划版本；证据校验、状态与恢复见 [任务计划](task-planning.md) |
| message | turn_id、message | 最终成功时追加的完整协议消息，包含工具请求和结果 |
| turn_completed | turn_id、status、reason、duration_ms、error | 完整成功消息块的提交标记 |
| media_saved | MediaRef v1：media_id、source、mime_type、size_bytes、version、created_at、schema_version | C00.2 宿主已发布完整二进制后的来源引用；只关联本轮输入/工具，不保存正文或路径，见 [媒体证据](media-evidence.md) |
| session_recovered | turn_ids、request_ids、tool_keys、uncommitted_messages、终态详情、backup_file、tail_repaired | 新获写锁后的恢复记录；遗留 Hook 另列 hook_ids；明确受影响操作及备份，不猜用量或命令结果 |

几个容易混淆的区别：

- request_usage 说明用量已经结算，不证明响应有效或整个轮次成功。
- request_finished 的 succeeded 表示有效模型响应；即使响应未报告用量，请求也可成功，但整轮仍按预算规则停止。
- turn_completed 只有在完整消息块通过校验并提交后才产生，表示轮次 succeeded；不能单独调用 finish_turn 声称成功。
- tool_finished 说明工具返回了结果；文件不存在、参数错误和用户拒绝也能返回结果，业务是否成功应读取 output。
- tool_started 说明进入工具处理，可能随后在参数校验或用户确认处结束；它不证明命令进程已经启动。命令的实际执行边界由 command_* 描述。

B14.2.1 的底层调用同样记录原模式、Hook 与批准；完整消息只提交根工具配对，嵌套结果通过父项关联，不能独立加入模型历史。父项必须等待全部子项完成；失败/中断恢复仍按既有规则保存未知状态，不自动重跑。详见 [代码编排](code-orchestration.md)。

## T4 执行模式记录

标准启动的聊天 request_started.context.execution_mode 保存实际 plan/act 模式，参与请求快照校验。模式说明加入当次 system 输入并计入预算，不改写会话 system_prompt 或成功消息；旧日志可没有该字段。子任务每次派发在 ChildRun.execution_mode 中独立记录，子请求记录同一模式，续聊可以捕获新主轮次模式。记录用于追溯，不用于恢复当前终端模式，也不会触发重发。

## E1 记忆选择记录

B07.2 的记录还可携带 derivation：整理与候选 ID、全部原记忆 ID/版本及 unverified 标记。它保存用户接受概览时的来源链，接受不代表已验证事实。旧记录省略该字段仍可读取；录制重放使用冻结的概览，不查询今日候选或原库。见 [记忆整理](memory-organization.md)。

B06.2 的 records 可携带 subject、valid_from、supersedes；身份和替代关系来自用户明确保存的版本，不能用相似度猜测。旧记录缺省仍可读，当前输入保留实际选中版本。见 [主体与时间](memory-identity.md)。

request_started.context.memory 是可选字段，strategy 默认为 keyword_v1，B06.1 增加 semantic_v1、keyword_fallback_v1 及严格校验的 retrieval 来源，见 [语义记忆](semantic-memory.md)；保存查询、选择状态、完整条目版本与来源。旧请求没有此字段仍可读取；已记录快照不会随记忆库未来修改或删除而变化。条目作为资料参与本次请求，不追加为普通 message；日志解释器复用 contracts/memory.py 校验记录格式。

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

若失败状态也无法落盘，程序保留最初的异常并附加受控说明，日志只反映最后成功写入的事实，可能仍为 in_progress。强制终止无法执行异常收尾；下次新取得写锁时由第 5 项恢复记录补 interrupted。第 6 项已使用实际 HTTP、强制终止与全新进程验证这一边界。

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
| model_input_invalid | 适配器发送前的输入格式、能力或媒体来源/版本校验失败；未发送 HTTP，可靠本地用量为零 |
| request_limit / tool_limit / session_token_limit | 达到本轮模型次数、工具次数或会话累计 token 限额 |
| no_progress | 连续同名、同参数、同类工具失败达到配置上限；工具结果附带宿主观察，已结算请求保持原状态，失败输入不加入历史 |
| context_input_limit | 省略完整旧轮次后，受保护输入加安全余量仍超限；本地拦截，尚未发起该次请求 |
| usage_unknown | 用量不可靠，按预算策略停止；请求本身可能已成功 |
| answer_save_failed / request_save_failed | 最终消息提交失败 / 请求用量与终态结算失败 |
| storage_error / command_log_error / execution_unknown | 存储故障、命令日志故障、已有命令结果未知 |
| interrupted / process_exit | 捕获到用户中断 / SystemExit |
| restart_recovery | 新获写锁发现上次缺少结束记录；不推断实际执行结果 |
| internal_error | 未预期的程序异常；只记通用说明，原异常继续传播 |

`agent_error`、`api_error`、`budget_exhausted` 是公共错误的兜底类别，具体业务边界优先提供表中的精确原因。错误说明只取公共契约中的受控文本，不复制原始系统异常、堆栈、服务端错误正文或未保存回答。

计时边界：

- 请求：request_started 写入完成后开始，到模型调用及响应解析完成或抛错为止；不含开始与结算日志的写入。
- 轮次：turn_started 写入完成后开始，到终态生成为止，包含工具执行、用户确认等待和中间落盘；成功时不含最终消息块自身的写入。
- 使用进程内单调时钟，不用两个 UTC 时间戳相减。UTC timestamp 仍用于定位事件发生时间。
- 强杀或终态写入失败时，没有可靠的结束记录；不补一个虚假的零耗时。跨进程恢复不能复用此前单调时钟起点，恢复产生的耗时为 null。

仍使用 v4 格式。早期 v4 缺少整组详情时可以读取，原因与耗时保持 null；只缺部分字段、负数/布尔耗时、成功携带错误等非法组合拒绝读取。旧 v1/v2/v3 规则不变。

## 只读开发诊断（第 4 项）

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m adam.diagnostics "C:\Users\你的账户\.adam\sessions\2026\10\28\12345678.jsonl"
```

路径换成实际会话文件。输出 JSON，按轮次组织原始输入、状态、原因、耗时、请求用量、工具参数/结果和命令详情。进行中或早期记录的未知字段显示 null。输入没有被提交为成功历史，也能作为诊断证据查看。

入口只读取指定文件，不加载模型配置、不访问 API、不创建数据/锁目录、不恢复或修改日志。成功退出码为 0，读取或格式失败为 1，错误写到 stderr。读取活跃文件时可能碰到追加尚未完成，明确报错；需要稳定快照时在聊天进程退出后读取。报告包含用户输入和工具结果，按原会话资料保管。

## 持锁恢复（第 5 项）

`/resume <ID>`、启动参数 `--resume <ID>` 或存储接口 select_session 新取得写锁后执行恢复。同一实例重复选择正在使用的会话不收尾当前操作；只读查询不恢复。

1. 校验全部完整行。仅未换行且 JSON/UTF-8 不完整的最后一行可清理；完整 JSON 缺换行则补齐后验证。中间损坏、非法完整事件和不受支持版本拒绝处理。
2. 计算尚无终态的请求、轮次与工具；部分成功消息必须是结构和关联均合法的前缀。
3. 有恢复动作才先把原始字节完整写入同目录 `<ID>.jsonl.<随机值>.bak` 并 flush/fsync。
4. 保留全部完整记录，追加 session_recovered；写入同目录临时文件、flush/fsync，然后原子替换。失败保留原日志，已生成备份不删除。
5. 回放恢复事件，把尚未结束的操作标为 interrupted，原因 restart_recovery，耗时 null。已成功/失败的状态不改写；已知用量保留，未知用量仍未知，命令执行结果未知仍阻止自动续跑。
6. 无提交标记的完整 message 行仍保留在日志中，仅清除待提交投影，不进入成功上下文。已有完整 turn_completed 的历史照常恢复。

恢复记录列出涉及的 ID、未提交消息数、尾行是否修复及备份文件名，可从只读报告的 recoveries 查看。重复重开不重复追加恢复事件或备份。恢复不会调用模型或执行工具；原子替换不承诺断电时目录项的持久性。只读列表遇到尚未恢复的残缺文件仍报错，可直接使用文件名 ID 执行 /resume。

## 写入顺序与失败边界

1. 保存输入后才进入模型请求流程，记录请求开始后才调用模型。
2. tool_started 保存成功后才执行工具，tool_finished 保存成功后才把结果交回模型。
3. command_* 继续独立记录确认和进程边界；后续模型失败不会删除命令证据。
4. 写入前验证轮次、请求与调用关联；非法引用、重复结束、跨轮结算或成功消息与原始输入/执行结果不一致时拒绝追加。
5. 未完成的诊断轮次或工具记录允许读取。缺失用量仍保持未知，只有开始事件的命令仍标为 unknown；相应的禁止继续调用规则保持不变。
6. 常规只读与追加仍严格校验；新取得写锁时按上述恢复策略处理尾行和遗留状态。可捕获的普通追加失败仍尝试回滚本次写入。

当工具结果写入失败时，真实工具可能已经执行；此时停止本轮，不把失败包装成可让模型重试的普通工具结果。

## 代码阅读顺序

- `adam/application/conversation.py`：开始轮次，关联模型请求，分别计时并记录结果，成功后提交完整历史。
- `adam/contracts/errors.py`、`adam/session/outcome.py`：稳定失败类别与共用终态字段，生成受控说明并校验状态组合。
- `adam/tool_calls/executor.py`：统一围绕所有工具记录开始和结果，衔接命令细节记录。
- `adam/session/storage.py`：公开存储接口、文件锁和追加落盘。
- `adam/session/log.py`：解析事件并验证关联，区分诊断记录与已提交历史。
- `adam/contracts/protocol.py`：校验工具消息协议和完整成功轮次。

## 验收

定向测试覆盖轨迹、状态与结果详情，不调用真实付费模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_trace.py tests/features/test_execution_states.py tests/features/test_execution_outcomes.py -v
```

检查以下结果：

- 第一次请求返回 read_file，第二次失败，原始输入、两次请求和读取结果仍可从日志关联。
- run_command 与 run_isolated_command 的通用工具记录和命令事件指向同一轮次、请求和调用。隔离开始事件保留 environment/distribution/input_files，结束不能改写；completed、timed_out、output_limited 与实际 reason 对应，非零不能作为计划成功证据。取消或通信故障保留未知结果，见 [隔离命令](runtime-isolation.md)。
- 成功链沿用输入时的 turn_id；失败输入不会混入后续有效上下文。
- 有效请求成功与整轮成功分开，未知用量仍保持未知；请求/工具阶段中断与程序错误有不同终态。
- 保存失败只结束轮次，不抹去请求成功；失败日志再次写入失败不掩盖原始异常。
- 通过固定时钟验证两次请求与整轮的精确毫秒数，分别覆盖成功和第二次请求 HTTP 503。
- 超时、连接失败、协议错误、中断、退出、程序错误有独立原因，原始错误内容不进入日志。
- 复用额度和保存失败场景核对原因；基础测试拒绝非法详情，早期 v4 的未知字段不伪造。
- 非法关联被拒绝后，原日志仍可正常读取。
- 输入记录失败不请求模型；工具开始记录失败不执行工具；工具结束记录失败不继续请求模型。

本轮自动化验收结果记录在 [练手 TODO](agent-practice-todo.md)。

### 真实故障验收（第 6 项）

`tests/features/test_process_crash.py` 启动本地 TCP HTTP 服务，独立 Python 子进程经过真实 LLMClient/Conversation 发起请求，父进程强制 kill，再由全新子进程恢复：

| 强杀位置 | 恢复结果 |
| --- | --- |
| 服务端收到请求、尚未响应 | 请求与轮次 interrupted；用量/耗时未知，续发被预算策略阻止 |
| 回答已取得、最终消息写到半行 | 请求 succeeded、已知用量保留；轮次 interrupted，原文件备份，残缺尾行清理，未提交消息不进历史 |
| 完整消息及提交标记已写入、尚未展示回答 | 轮次仍 succeeded、回答可恢复，不追加多余恢复记录 |

写入中场景用测试代理在真实文件写入边界暂停，flush/fsync 部分字节后强杀；不是直接拼造重启输入。每个场景都检查服务端只收到一次请求、旧成功历史保留。辅助进程代码位于 `tests/features/support/crash_worker.py`，与产品代码分开。

普通成功、API 失败和真实 fsync 保存失败沿用已有业务场景。最终全量 **207 passed in 37.00s**；Ruff 格式、lint 和 Pyright 通过。测试不访问付费模型；不声称覆盖物理断电或全部操作系统/文件系统故障。

## 旧会话

v4 明确拒绝旧 v1/v2/v3，不自动迁移或删除。旧文件留在当前会话目录会使 `/sessions` 报错；可自行将旧文件归档到会话目录之外，或者把 `config.toml` 中的 `storage.sessions_dir` 改为新的目录，再新建会话。程序不会代替用户移动旧数据。

## V4.1 文件编辑记录

编辑使用 edit_id，不能只通过 tool_started 判断文件已经修改。edit_planned 保存原版本、目标版本与 diff；edit_started 表示获批且准备实际写入，缺少结果时状态 unknown；edit_finished 记录 applied、denied、conflict 或 failed。只有 applied 携带实际 after_version，且必须匹配计划目标。结果必须关联同一个轮次、请求、工具和编辑。

恢复事件在存在未开始计划时增加 edit_ids，并将这些计划标记 not_started。已经 started 的 unknown 不改写、不猜测，也不自动重跑；它会阻止普通续聊和手动摘要。现有命令记录规则保持，用户拒绝命令/编辑则以 approval_denied 结束本轮，跳过同批后续工具。

独立诊断在每轮增加 edits。完整编辑记录可以存在于失败或中断轮次，不能因为聊天历史未提交而当成文件没有变化。测试覆盖真实文件替换前后强杀，见 [文件修改与验证](file-editing.md)。

## T7 检查点与撤销

标准编辑在获批后、edit_started 前保存独立原字节快照；内部 edit_checkpoint 回调不写入 JSONL。用户 /undo 先记录来源，再保存逆向 diff；批准后的 undo_started 才表示进入写入边界。undo_finished 分别记录成功、拒绝、冲突、失败或写入前中断；只有 applied 携带恢复后版本。原编辑和聊天历史不被改写。

重启恢复对尚未开始的撤销增加 undo_ids 并标记 interrupted，已开始的 unknown 保持未知。统一副作用查询阻止未确认状态继续请求模型；失败输入仍可作为诊断保存。独立诊断在顶层列出 undos；数据位置、权限与详细契约见 [修改检查点](edit-checkpoints.md)。T7.3 已通过真实进程强杀、物理半行和恢复发布中断验收；目标文件即使已恢复，缺少 undo_finished 仍保持 unknown，不根据内容补造成功。

## B04.2 程序状态

B04.2 的聊天请求可包含 runtime_state，格式 runtime_state_v1。它保存输入构造时的完整只读状态，模型获得同一对象的有界表示；请求重试可复用该状态。解码验证字段、计数、派生余量、排列和执行模式，旧记录缺省时不补造；摘要和反思沿原协议。详见 [程序状态](runtime-state.md)。

## B03.2 消息排列版本

聊天请求的 request_started.context 新增可选 layout_version=stable_prefix_v1，表示系统/模式与目录在前、历史后提供本轮资料，再保留完整当前工具链。缺省字段仍按旧选择记录校验；未知版本拒绝读取。它不同于 input_prefix.layout_version 的字节编码版本，也不改变 recent_turns_v1 的历史选择算法。

## B03.1 可选模型观测

request_usage 和 compaction_usage 可携带 cache_usage/input_prefix，字段缺省兼容旧 v4 并保持未知；有字段时严格验证格式、计数、模型与前缀长度。服务端缓存计数不减少会话 token 预算，本地前缀表示输入准备，不证明请求已发出或缓存命中。独立诊断直接展示投影；详细口径见 [缓存观测](cache-observation.md)。

## V3.3 / V3.4 独立摘要事件

- compaction_started 保存 operation_id、可选触发 turn_id、模型、来源轮次、前一摘要 ID、请求输入估算与输出上限；成功写入后才调用 API。自动摘要携带触发轮次 ID，手动 `/compact` 不创建用户轮次。
- B05.1 新操作还保存 compaction_version=task_aware_v1 与 current_task。自动操作的任务快照必须对应真实本轮输入和已完成工具前缀；手动操作为空。它不增加摘要覆盖，不使失败轮进入有效历史；缺少新字段的旧事件仍可读，伪造来源或未知版本拒绝。详见 [任务导向压缩](task-aware-compaction.md)。
- pruned_tools 记录自动摘要素材的缩减来源、算法及长度，使用和正常请求相同的来源校验；原始工具结果不改写。
- compaction_usage 在同一事件结算该请求的终态与用量；请求成功不表示摘要已经提交。
- compaction_completed 保存完整摘要及成功终态，只有此事件使新摘要生效；compaction_finished 只允许失败/中断。
- 摘要操作不生成普通用户消息或成功问答。原始 message 保留，后续请求只通过摘要 ID 引用其历史替代视图。
- 未结算摘要请求和缺失用量纳入会话未知消费。session_recovered 在有遗留摘要时增加 compaction_ids，关闭状态，不重发或猜测费用；较早 v4 的恢复事件仍可读。
- 独立诊断报告新增 compactions 列表，显示素材来源、请求及操作状态、耗时和用量。普通 requests 列表不混入摘要操作；计算该轮总模型调用次数时，需加上 turn_id 相同的摘要请求。新事件沿用 v4 扩展，旧程序不保证能够读取。
## T11.1 修改提案记录

proposal_submitted 在本子轮 submit_proposal 进行中保存，read 指向同子轮已完成 read_file 的实际输出。路径、版本和可见行不由模型自报；读取/提交来源与参数在追加和回放时共同校验。事件沿用 v4，旧程序不保证可读取。

候选落盘后退出，即使工具结果或子轮尚未成功提交，也保留候选供诊断；原恢复只结束遗留执行，不应用文件、不自动重发。load_proposals 返回保存事实，T11.2 主任务入口另行验证实际委派及成功子轮。回放不读取当前源文件；应用时必须重新核对权限、原版本和目标版本。

## T11.2 主任务提案应用

收集结果沿用普通 tool_finished，保存模型实际看见的有界页面。主会话 edit_planned 增加可选 proposal，保存完整不可变候选；只有 apply_proposal 允许携带，且须与实际工具参数、所属主会话、原主轮次、路径及前后版本相符。

后续 edit_* 与原检查点流程保持；edit_finished 不能改写候选。实际 applied/unknown 状态阻止同候选重复应用，包括先撤销再重做。回放核对已保存的父来源和版本，不重新打开子日志或执行修改；工具动作前由主服务另行验证真实完成的子派发。两个入口共用编辑额度、计划完成证据及执行报告。

## T11.3 提案与编辑恢复验收

真实进程在候选半行、候选保存、子成功提交、主编辑计划/开始/实际替换/结束及主成功提交八个边界强杀后恢复。
候选存在不等于子轮成功；父编辑 started 无 finished 仍保持 unknown，不以文件当前内容升级成功。
已保存 applied 即使主轮中断也保留，可显式撤销。重复重开不请求模型、不批准、不写目标；完整矩阵见 [提案验收](edit-proposals.md)。

## T12.2 实际工具说明

request_started.context.tool_schema 可选记录 names 与 utf8_bytes，来自实际该次 RequestContext.tools；大小按 ensure_ascii=False 的默认 JSON 编码统计，不等于 HTTP 字节或 token。整段输入估算仍在 estimated_input_tokens，旧记录缺省保持未知。工具开始/结束继续绑定原 turn_id、request_id、tool_call_id；同批发现不能扩展触发本批请求的工具名单。

## C04.2 专用视觉请求

独立显式单图入口仍使用原 request_started/request_usage/request_finished、turn_started/turn_completed/turn_finished；不新增事件类型或版本。request_started.context.image_question 是可选证据：version=1、backend=llama_cpp、images 为与当前 turn_started.media 完全相等的一张原图、cost/currency=null、cost_status=unpriced。有字段时 contracts.vision 严格校验，不允许换图或伪造零价；旧请求无字段仍兼容。

该段表示输入来源和无可核实价格，不是已计费图片量；实际 token/未知用量、请求耗时、超时/取消原因继续以原结算事件为准。成本未知不冒充零，成功请求也可能因未知用量或最终保存失败而整轮失败。专用入口恢复既有子日志消费并共用预算预留，不自动重发模型或子任务。使用、真实失败及与冻结录制的区别见 [视觉问答](visual-question-answering.md)。
