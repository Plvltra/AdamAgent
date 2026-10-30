# 本地生命周期 Hooks（T5）

状态：T5.1 配置、事件协议和独立进程执行器已实现；普通聊天的工具前后接入、审计和两个可运行示例分别留在 T5.2、T5.3。
当前设置 hooks.enabled 不会让现有工具循环自动执行检查器，必须显式调用 HookRunner。完整范围见 [能力扩展任务](agent-maturity-tasks.md)。

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
T5.3 将提供可运行示例，此处不创建虚构的检查脚本。

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

T5.2 将把结果连接到工具策略和会话记录：前置失败阻止动作；后置失败保留原结果并明确报告。当前未将这部分标为完成。

## 验收

- tests/features/test_hook_process.py：13项真实进程场景，覆盖允许/拒绝/观察、环境和工作目录、关闭/缺省、坏响应、非零退出、超时、输出上限、输入过大、程序不存在与阻塞输入取消。
- tests/test_hooks.py：19项配置和协议边界，包含实际 TOML 加载、重复键和后置响应不能改写决策。
- tests/test_architecture.py：新增 hooks 包依赖方向约束，仅依赖自身、config 和 contracts。

上述与架构共51项定向检查通过；全量 Ruff、Pyright 与 pytest 随本项提交执行。全部使用本地临时程序，不请求付费模型。
