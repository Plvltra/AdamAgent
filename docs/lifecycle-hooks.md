# 本地生命周期 Hooks（T5）

状态：T5.1～T5.3 工程实现及离线验收完成。启用并配置事件后，主/子工具循环会执行检查器；两个可运行示例见 [示例说明](../examples/hooks/README.md)。完整范围见 [能力扩展任务](agent-maturity-tasks.md)。

## 已确定的边界

- 默认关闭，每种事件最多配置一个本地检查器。配置由用户提供，启动后使用冻结值，工具或模型响应不能选择检查程序。
- before_tool 返回 allow 或 deny；程序故障、超时、坏响应均属于 failed，不视为 allow。
- after_tool 接收实际结果，只返回观察说明，不能要求将已执行动作改为未执行。
- 检查器不是模型工具，不递归触发自身；当前基础模块不持有工具注册表、会话存储或模型客户端。
- 检查器在工作目录运行，使用 argv 启动，不隐式执行 shell；Windows 使用隐藏进程。
- 不传入模型连接配置，只继承 PATH、系统/用户目录和临时目录等基础环境项。Hook 仍是用户信任的本地程序，不提供操作系统沙箱。

## 配置格式

主 config.toml 可包含：

~~~toml
[hooks]
enabled = false
timeout_seconds = 5
max_input_bytes = 65536
max_output_bytes = 8192
~~~

启用时必须至少提供 hooks.before_tool 或 hooks.after_tool；每个子表只包含 argv 字符串数组，例如程序路径和检查脚本路径。
相对程序路径按实际工作目录解析，普通程序名经 PATH 查找；建议指定所需 Python 的路径，避免调用系统中其他版本。
[配置模板](../examples/hooks/config.toml) 使用实际存在的检查脚本：前置限制指定目录的专用编辑，后置检查已修改 Python 文件的语法。模板保持默认关闭，使用前确认解释器和脚本路径。

超时支持 0.1～60 秒；输入上限支持 256～1048576 字节；每条输出流上限支持 256～65536 字节。输入不能静默截断，stdout 与 stderr 分别有界。
关闭状态仍校验配置，加载配置不启动检查器。

## 输入与响应协议

stdin 是一个 UTF-8 JSON 对象，发送后关闭 stdin。字段为：

| 字段 | 来源与含义 |
| --- | --- |
| version | 固定为 1 |
| event | before_tool 或 after_tool |
| workspace | 实际工具工作目录 |
| execution_mode | 程序当前 plan/act |
| source | session_id、turn_id、request_id、tool_call_id |
| tool | name 和已解析的 arguments |
| result | 仅 after_tool，携带真实工具结果 |

stdout 必须是唯一完整 JSON 对象，退出码必须为零。诊断写 stderr；它不能替代协议结果。

前置允许示例：

~~~json
{"version": 1, "decision": "allow", "reason": "检查通过"}
~~~

拒绝时将 decision 改为 deny。reason 可省略；有值时最多2000字符。

后置观察示例：

~~~json
{"version": 1, "note": "已记录本次执行结果"}
~~~

后置响应不能包含 decision。重复键、未知字段、错误版本、非有限数值、非法 Unicode 或额外散文均拒绝，不自动修补。

## 结果与故障

HookResult 区分 allowed、denied、observed、failed，并保留受控原因码、退出码、耗时和有界 stderr。
关闭或未配置当前事件时返回 None，零进程副作用。

失败原因包括 invalid_input、start_failed、timed_out、output_limit、io_failed、nonzero_exit、invalid_response。
取消信号在清理进程后继续向上传递，不转换成通过，也不自动重试。
输入和输出分别由线程传输，因此检查器不读取 stdin 时，协调线程仍能执行超时或取消；进程树清理是尽力操作，直接子进程会回收。

## 工具流程与审计

每项实际顺序为：tool_started → 参数/模式/规则检查 → 前置 Hook → 工具自身权限与批准 → 实际执行 → tool_finished → 后置 Hook → 回传模型。

T6.2 允许同批安全读取的动作重叠；ToolInvocation 固定每项来源与检查快照。预检、Hook 和所有日志仍在协调线程运行，结果按原调用 ID 顺序保存与回传；编辑和命令保持串行。

- 参数、模式或规则未通过时，不启动检查器。前置 Hook 通过不能绕过后续工具权限与人工批准。
- 前置拒绝或失败返回 hook_blocked，本次工具不执行；不混同用户拒绝整轮，也不触发自动反思。模型可以在原有额度内调整后续动作。
- 后置接收已落盘的实际回传 JSON，包括失败、拒绝和被限长的结果。它不是不受限的完整原始输出；输入超过 Hook 上限时记 invalid_input，不截断输入冒充检查成功。
- 后置协议或进程失败写入独立审计并提示用户，模型仍收到原工具结果。子任务使用自己的来源与日志，故障提示带子任务 ID。
- hook_started / hook_finished 关联同一 turn_id、request_id、tool_call_id，另有 hook_id、event、execution_mode；结束记录保存检查状态、原因、耗时和有界 stderr。工具名称、参数与结果引用已有工具记录，不复制为聊天消息。
- 检查开始记录保存成功才运行程序；任何 Hook 审计失败以 hook_log_error 停止本轮，不让模型自动重试。后置审计失败仍保留此前已经落盘的 tool_finished 以及命令/编辑记录。
- 捕获取消时清理进程并尽力保存 interrupted / cancelled；保存失败不覆盖原中断。重开会话先备份，再通过 session_recovered 把遗留检查标为 interrupted / restart_recovery，耗时未知，不自动重跑。
- 尚有未结算检查时不能提交整轮成功。失败输入和 Hook 审计不会直接进入有效聊天历史；可通过 SessionStore.load_hooks 或独立诊断入口 python -m adam.diagnostics 查看每轮 hooks；诊断只读取已有日志。
- Hooks 不注册为模型工具，程序不会为 Hook 再派发 Hook；关闭时不生成 Hook 事件。

## 验收

- tests/features/test_hook_process.py：13项真实进程场景，覆盖允许/拒绝/观察、环境和工作目录、关闭/缺省、坏响应、非零退出、超时、输出上限、输入过大、程序不存在与阻塞输入取消。
- tests/test_hooks.py：19项配置和协议边界，包含实际 TOML 加载、重复键和后置响应不能改写决策。
- tests/test_architecture.py：新增 hooks 包依赖方向约束，仅依赖自身、config 和 contracts。

- tests/features/test_tool_hooks.py：14项主/子真实流程场景，验证前后来源、模式、审批、拒绝、后置事实保留、四种日志失败和进程取消。
- tests/test_hook_log.py：17项关联/状态与恢复边界，验证非法追加不改变日志、未结算检查不能提交成功。

- tests/features/test_hook_acceptance.py：13项实际示例组合验收；保护目录、合法/非法语法、人工拒绝、关闭、模式、项目规则、外部改动、日志及展示故障。
- tests/features/test_cancellation.py：保留原6项真实 CLI/HTTP 取消场景，新增前置/后置检查中取消；检查进程回收，已完成命令仍为 completed，模型请求可靠用量保留。
- 独立诊断仍只读，新增每轮 hooks 投影；对应旧诊断场景已回归。

T5.3 新增15项业务场景。定向检查通过，完整 Ruff、Pyright 与 pytest 随提交执行。全部使用本地临时程序和模拟模型，不请求付费 API；不把这些流程测试当作真实模型遵从效果的证明。
