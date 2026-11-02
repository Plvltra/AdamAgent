# B20 修改后诊断

B20 工程任务已完成：结构化检查、版本观察、模型回传、输入预算、来源日志、离线重放和组合故障。修正通过模拟 HTTP 驱动真实文件与检查进程；没有调用付费模型，不据此宣称真实模型能可靠诊断和修复任意代码。以下保留分步实现与验证依据。

## 两种结果分别记录

编辑得到 applied 表示文件已修改；Hook observed 表示检查器正常交付报告；报告的 failed 表示在指定版本发现问题。三者可以同时成立。检查器进程非零退出、协议损坏或超时仍属于 Hook failed，不能把它解释成文件语法错误或编辑没有发生。原批准、检查点、撤销及存储失败停止策略保持。

## 响应与边界

输入保持 Hook version=1。原前置和后置响应仍可用；新增后置响应 version=2，顶层只接受 version、note、diagnostics。诊断对象字段如下：

| 字段 | 约束 |
| --- | --- |
| checker | 1～64字符检查器名称，字母/数字/下划线/点/横线 |
| path | 本次编辑结果中的原路径，不能替换为其他文件；最多4096字符 |
| expected_version | 已成功编辑的 after_version，64位小写 SHA256 |
| checked_version | 检查器实际读到字节的 SHA256；读取不可靠时为空 |
| status | passed、failed、stale 或 unavailable |
| items | 最多16项；每项 line/column 从1开始，column可为空；severity 为 error/warning/info，code 有界，message最多500字符 |

passed/failed 只能针对 expected_version；failed 至少有一项 error，passed 不得含 error。stale 必须读到不同版本，unavailable 必须没有可靠版本，两者均不提供冒充当前问题的诊断。报告总计最多16KiB UTF-8；仍先受配置中 Hook stdout 上限约束，默认8192字节。任一超限整体拒绝，不悄悄丢诊断；控制字符、非法 Unicode、未知字段和重复键同样拒绝。

宿主 HookRunner 核对报告与 replace_text/apply_proposal 的实际成功结果；SessionLog 写入和重启投影再次核对。日志保留原轮次/请求/工具/Hook关联，不读取文件来改写旧事实。报告是受信本地检查器的观察，格式和来源校验不证明检查器本身正确，也不能授予工具权限。

## 使用示例

[after_python_diagnostics.py](../examples/hooks/after_python_diagnostics.py) 只用标准库，最多读取1MiB并核对版本，用 ast.parse 检查，不执行目标代码。语法错误作为正常协议的 failed 报告返回；文件变化标记 stale，读取失败或过大标记 unavailable。原 [v1示例](../examples/hooks/after_python_syntax.py) 保留，便于比较协议演进。

在已有用户配置中，显式将 hooks.after_tool.argv 指向所用 Python 解释器和新脚本的绝对路径即可；复用原 timeout_seconds 和输入/输出限制，不另加开关。项目不会自动修改真实用户配置。

报告可从 SessionStore.load_hooks 或 python -m adam.diagnostics 的既有只读日志入口查看。B20.1 交付时仅保存报告；B20.2 已加入独立版本观察与请求预算，原报告仍不声称文件此刻必然未变。

## 验证

[6项业务场景](../tests/features/test_edit_diagnostics.py) 经过模拟模型 HTTP、实际编辑和独立检查进程：正确/错误语法、文件外部变化、读取超限、跨文件伪造及日志来源篡改；目标代码的顶层副作用不会执行，重启不修改已有事件。[22项契约边界](../tests/test_diagnostics.py) 覆盖结构、状态、位置、文本、总字节、来源和旧协议。新增共28项，相关126项通过（45.16秒）；原 Hook 示例与过程测试保持。

初次运行的超大参数自动测试ID导致 Windows 测试环境设置失败，改用短案例名后复验通过；这是测试命名问题。Ruff、557个文件格式、Pyright与注释差异分别核对；主线按 AGENTS.md 三级验证规则执行。没有调用付费 API。


## B20.2.1 当前轮次的诊断观察

DiagnosticContextReader 读取本会话 Hook 事实，只选择指定轮次、每个路径最后一份正常交付的报告。优先最近文件，最多4份且完整记录最多16KiB UTF-8；其他文件明确计入 omitted_count，不读取被省略文件。原报告和消息历史不改写。

DiagnosticFiles 复用 PathPolicy 和有界 UTF-8 读取，只返回 current、changed 或 unavailable。版本在读取前后分别检查路径权限，凭据、保护目录、撤销授权、读取失败、超限和非文本均不能提供当前故障。取消与存储故障继续传播。current 仅指观察时版本相同，不锁住文件，也不保证之后仍未变化。

原报告已经 stale/unavailable 时，不再重查并标记 not_checked，不能将后来读到的新文件解释为原检查结果。所有观察保留 Hook/工具/会话/轮次来源；供模型使用的消息只展示 current 的问题列表，其他状态保留来源和状态但不展示过期问题。原证据中的问题仍可追溯。

B20.2.1 交付时提供独立查询与消息契约；B20.2.2 已接入 Conversation，具体调用及失败边界见下节。

[7项业务场景](../tests/features/test_diagnostic_selection.py) 使用真实编辑与检查进程，覆盖修复后的最新报告、重启不写日志、文件变化/删除/超限、权限撤销、取消/存储故障、6个文件取4个、总字节省略和检查器无法核对版本。[16项契约边界](../tests/test_diagnostic_context.py) 校验身份、来源、重复、状态和容量。新增23项、相关100项通过（57.17秒），Ruff、563个文件格式及 Pyright 通过；新增代码和说明直接阅读审查，没有删除既有注释。主线仍执行全量提交门禁，无付费请求。


## B20.2.2 实际请求、预算与重放

用户启用后置 Hook 时，bootstrap 为主/子执行器注入各自的 DiagnosticContextReader，复用对应工作目录、权限、保护目录与文件大小上限。子任务仍受原只读/提案工具限制，不因诊断观察获得编辑权。仅启用前置 Hook 或完全关闭时，不增加查询。

Conversation 每次构造真实普通请求时选择本轮报告；自动摘要完成后重新观察版本。ContextBuilder 将独立 user 资料消息放在完整本轮工具链之后，明确它是程序观察而非用户指令或授权；工具回包、有效聊天历史及系统约束保持。没有报告时不增加消息，下一轮不会继承上一轮诊断。

诊断整体计入 diagnostics_tokens、压力判断、近期历史选择及输入上限。受保护内容放不下就停止，不偷偷删问题或额外请求模型；已有编辑仍属成功执行。/context 基础预览没有正在执行的轮次，不读诊断文件，估算中对应项目为0。实际请求证据保存在 request_started.context.diagnostics，可由原诊断入口读取。

SessionLog 写入与重启逐事件验证会话、轮次、最后一份 Hook、工具来源、报告全文及省略数；不匹配就拒绝，不写坏日志，也不调用下一次模型。freshness 是当时的独立观察，重启不以当前文件覆盖它。存储查询和保存故障停止流程；格式错误不解释成无报告。

录制增加可选 diagnostic_context_version=edit_diagnostics_v1 与 context/diagnostics 观察。离线适配器恢复原观察并比较调用来源，仍运行真实 ContextBuilder 和 SessionLog，不读源文件、不运行检查器。旧录制没有该版本时保持原消息和估算；未知版本或旧布局带新诊断拒绝。

[11项新增业务场景](../tests/features/test_diagnostic_request.py) 覆盖真实错误语法→修复→通过、输入/日志对应、外部改动、五类来源篡改、预览/下一轮隔离、旧录制、存储故障、实际预算停止以及禁止网络/进程/文件观察的重放。[契约测试](../tests/test_diagnostic_context.py) 新增2项整体预算和旧布局边界。相关153项通过（123.63秒），Ruff、564个文件格式与 Pyright 通过。初次两处失败来自错误文本预期及单轮录制器误用于下一轮，已修正测试场景；没有放宽生产校验或权限。原历史覆盖契约注释保留并补充诊断；原测试中的“尚未回传”说明随阶段更新。主线提交仍运行全量门禁，无付费调用。


## B20.3 组合验收

[故障场景](../tests/features/test_diagnostic_faults.py) 新增5项，继续实际运行本地检查器、编辑和 JSONL，远端使用模拟 HTTP。

| 情况 | 验证结果 |
| --- | --- |
| 检查器 stdout 超过512字节 | Hook failed/output_limit；原编辑 applied，模型不收到残缺诊断，终端有失败提示 |
| stdout可容纳但诊断超过16KiB | Hook failed/invalid_response；没有半份问题列表或伪造检查通过 |
| 报告写入后首次fsync失败 | 实际写入回滚，原编辑保留；Hook缺少结束记录，重启标中断，不重做检查或编辑 |
| 下一请求的诊断证据写入后fsync失败 | 新请求记录回滚且没有第三次HTTP；已保存报告和编辑保留，轮次失败 |
| 检查器收到编辑结果并报告PID后取消 | 真实进程回收、Hook与轮次中断、已知24token保留；重启不重新执行 |

两处保存失败只注入首次 fsync，回滚与后续终态写入允许成功；这验证可捕获IO故障，不代表断电持久性。重启两次仍只保留一次编辑，不将未保存的失败输入加入有效上下文。取消令牌只在测试装配中注入检查器，进程实际启动、阻塞并被回收，未用伪造开始事件代替。

成功闭环沿用 [B20.2请求场景](../tests/features/test_diagnostic_request.py)：编辑为错误语法，真实检查给出行号，模拟模型收到该版本问题后提出修复，实际重新检查通过；记录与模型输入逐一对应，重放禁止网络、检查进程和源文件查询。相关148项通过（108.10秒），Ruff、565个文件格式和 Pyright 通过。新增测试全文及注释已审查，无生产结构变更或原说明删除；主线按 AGENTS.md 三级验证规则执行。

个人练习可显式启用新检查器，让实际模型修改一个临时练习文件，核对 request_started.context.diagnostics、实际差异及最终测试。自动测试证明流程和边界，真实模型是否能选对修复仍需个人验收，不能把语法通过当作业务正确。
