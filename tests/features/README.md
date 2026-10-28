# 业务功能测试任务

更新日期：2026-10-28

这里按用户可见的能力组织自动化验收。功能模块改名后，仍能按“会话恢复”“命令执行”等业务名称找到对应测试。每个文件包含可独立运行的 pytest 场景；参数化场景展开为多个用例。

## 已实现功能与测试入口

| 功能点 | 要验收的业务结果 | 测试文件 |
| --- | --- | --- |
| 多轮聊天与模型切换 | 历史正确传递，新会话隔离；切换模型保留历史；终端指令不作为用户问题发给模型 | [test_conversation.py](test_conversation.py) |
| 配置启动 | 两份 TOML 与命令行覆盖可用；主配置或模型配置错误时在创建 HTTP 客户端前停止 | [test_startup_configuration.py](test_startup_configuration.py)、[test_session_lifecycle.py](test_session_lifecycle.py) |
| 会话保存与恢复 | 跨启动恢复、不串话、失败切换不丢当前会话；回答已生成但保存失败时仍可见且不重试 | [test_session_lifecycle.py](test_session_lifecycle.py) |
| 工具调用与预算 | 模型请求工具、执行后回传、再回答；工具错误可修正；单批额度不足不执行；同轮 ID 唯一、跨轮可复用 | [test_tool_loop.py](test_tool_loop.py) |
| 通用命令 | 确认后才产生副作用，拒绝不执行；退出码、超时、输出限制、环境变量隔离与执行日志符合契约 | [test_command_execution.py](test_command_execution.py) |
| 终端角色与按需用量 | 默认不刷 token，/usage 不请求模型且跟随当前会话；颜色只影响前缀，重定向退回纯文本 | [test_terminal_interaction.py](test_terminal_interaction.py) |
| V3.3 手动摘要 | 分批来源、旧版本保留、消费、未知用量、近期保护、真实强杀恢复 | [test_compaction.py](test_compaction.py)、[test_process_crash.py](test_process_crash.py) |
| V3.2 完整轮次选择 | 连续后缀、原日志保留、实际请求来源、重启一致、非法来源拒绝 | [test_context_selection.py](test_context_selection.py) |
| V3.1 上下文预览与输入预算 | 只读预览、完整工具历史、会话/模型切换与重启；输入/历史/工具说明超限无请求，工具结果增长后再次检查 | [test_context_preview.py](test_context_preview.py) |
| 统一失败反馈 | 新错误类型无需修改 CLI；预算分类准确；磁盘失败保留回答；终端和程序故障继续传播 | [test_error_feedback.py](test_error_feedback.py) |
| V1.3 第 2 项请求/轮次状态 | 请求成功不等于轮次成功；已知用量独立、中断收尾、原始异常保留 | [test_execution_states.py](test_execution_states.py)、[test_error_feedback.py](test_error_feedback.py) |
| V1.3 第 3 项原因与耗时 | 两次请求和整轮分别计时；API、预算、中断和保存失败的原因准确，原始错误内容不入日志 | [test_execution_outcomes.py](test_execution_outcomes.py)、[test_tool_loop.py](test_tool_loop.py)、[test_error_feedback.py](test_error_feedback.py) |
| V1.3 第 4 项开发诊断 | 只读报告关联输入、请求、工具和失败详情；真实模块入口、未知字段和无效文件不产生写入 | [test_diagnostics.py](test_diagnostics.py) |
| V1.3 第 5 项持锁恢复 | 备份后修复尾行、保留完整记录、状态不误判、未知用量/命令不重跑；失败保留原文件和旧锁 | [test_restart_recovery.py](test_restart_recovery.py) |
| V1.3 第 6 项真实进程中断 | 本地 HTTP 请求后强杀，全新进程恢复；请求中、消息写入中和提交后均不重发，历史与用量准确 | [test_process_crash.py](test_process_crash.py) |
| V1.3.1 执行轨迹 | 失败也保留输入/请求/全部工具结果；关联正确，日志失败停止后续动作，诊断不污染成功历史 | [test_execution_trace.py](test_execution_trace.py) |

这里也保留直接验证业务边界的场景，例如命令超时、请求额度耗尽；不强求每个场景都从 CLI 启动。
基础校验仍在上层：`tests/test_config.py`、`tests/test_llm_config.py`、`tests/test_llm.py`、`tests/test_storage.py`、`tests/test_tools.py`。细粒度协议、路径和数据校验不重复复制到业务测试中。

## 本次补充的任务

- [x] 配置错误时，真实 main 入口在 HTTP 客户端创建前失败。
- [x] /usage 在会话切换和重开后显示所属会话的用量。
- [x] 命令批准/拒绝通过临时文件是否真实写入验证，而不只检查返回文字。
- [x] 目录/文件工具报错后，模型收到错误并改用正确参数完成任务。
- [x] 最终回答的用量日志保存失败时，显示未保存回答，重开后保留未知用量。
- [x] 命令已执行但结束日志写入失败时，不再次执行、不请求模型；重开后结果未知。
- [x] 一次请求多个工具而剩余额度不足时，整批工具均不执行。
- [x] 不同成功轮次复用相同工具调用 ID，记录和恢复仍分别归属正确轮次。
- [x] 子进程不能读取父进程设置的测试用 API 密钥环境变量。

V1.3.1 与测试整理验收：原有 125 个用例保留，补充 11 个用例，共 136 个。

本次错误契约验收（2026-10-28）：保留原 136 个场景，按公开契约调整故障替身和预算异常断言，新增 13 个用例；全量 **149 passed in 23.22s**。

## 统一错误契约的补充验收

- [x] CLI 未导入的新业务异常也能展示提示；存在未保存回答时一并展示。
- [x] 已知额度耗尽停止请求，显示“执行受限”，不误报模型请求失败。
- [x] HTTP 失败只显示一次错误分类，不泄露远端响应正文。
- [x] 在真实消息提交的 fsync 边界注入失败，回答可见、历史回滚且不重试。
- [x] 初始界面及回答展示阶段的终端 OSError / TypeError 均原样传播，已保存回答保留、会话锁释放。
- [x] 会话读取故障转换为公共失败并保留原因链，原始系统详情不进入提示。
- [x] 存储内部类型错误继续传播，回滚后文件保持原样。
- [x] 实际启动入口统一展示存储目录创建失败和工具工作目录无效。

文件 IO 故障在操作边界注入 OSError；替换公开存储接口时遵循其 StorageError 契约。既有命令结束日志失败测试继续验证不重执行、不请求下一次模型及重开后的未知状态。

## 第 2 项：请求与轮次状态验收

- [x] 通过同一轮两次请求验证：第一请求成功、第二请求失败、轮次失败。
- [x] 请求有效但用量未知时，请求成功、轮次失败；正常成功仍提交完整历史。
- [x] 请求中断或程序错误分别记 interrupted / failed，原始异常对象继续传播。
- [x] 工具阶段中断不改写此前模型请求的成功；已结算用量保留。
- [x] 复用预算停止与真实 fsync 故障场景，验证请求与轮次终态独立。
- [x] 失败状态自身写入失败时保留原 API 错误，日志保持最后已持久化的事实。
- [x] 拒绝重复终态、失败请求触发工具、失败请求提交成功轮次及绕过消息提交的成功标记。
- [x] 旧 v1/v2/v3 均拒绝读取且文件不被改写。

第 2 项验收（2026-10-28）：保留原 149 个用例并扩展状态断言，新增 18 个，全量 **167 passed in 26.12s**；Ruff、格式检查与 Pyright 通过。

第 2 项只验证状态，第 3 项补齐原因与耗时；两者都不代替自动重启收尾或强杀恢复。

## 第 3 项：原因与耗时验收

- [x] 固定单调时钟验证两次请求分别 125/250 毫秒，整轮含工具阶段共 400 毫秒；覆盖成功与第二次请求 HTTP 503。
- [x] 超时、连接失败、协议错误、中断、退出和程序错误有稳定原因，耗时正确，原始细节不写日志且不重试。
- [x] 复用额度与保存故障场景，区分请求/工具/token 上限、用量未知、请求结算失败与回答提交失败。
- [x] 基础存储测试拒绝负数、布尔/NaN 耗时、成功携带错误及不完整字段；早期 v4 缺少详情保持未知。

第 3 项验收（2026-10-28）：新增 15 个用例，全量 **182 passed in 29.84s**；Ruff、格式检查与 Pyright 通过。

第 4 项新增 5 个业务场景，定向 **5 passed**；Ruff 与 Pyright 通过，全量回归随本项提交检查执行。

第 5 项新增 17 个恢复业务场景，全量 **204 passed in 27.87s**；Ruff 与 Pyright 通过。这里手工制造遗留记录及 IO 故障；真实进程强杀由下面第 6 项单独验证。

第 6 项新增 3 个真实进程场景，辅助 worker 位于 `support/crash_worker.py`；写入代理只负责暂停在半行落盘位置，HTTP、进程 kill 和重启均实际执行。结合已有成功、API 失败和 fsync 保存失败场景，全量 **207 passed in 37.00s**；Ruff 格式、lint 和 Pyright 通过。

## V3.1 上下文验收

- [x] `/context` 仅概览，`/context full` 展开消息/工具；不创建会话、不发模型请求、不泄漏连接密钥。
- [x] 预览保留成功工具链与推理字段，跟随会话、模型及重启；不冒充最近请求，失败输入不混入历史。
- [x] 输入、历史或工具说明超限时没有 request_started，保留轮次失败原因，不新增未知用量。
- [x] 真实文件工具使当前轮次增长后，下一次请求被拦截；此前请求/工具证据和已知用量保留。
- [x] 实际 CLI 启动读取 TOML 配置，公共错误展示沿用既有契约。
- [x] 基础测试 `../test_context.py` 覆盖协议保真、副本独立、临界值、中文与工具 Schema 估算；`../test_config.py` 覆盖默认值、覆盖配置及非法值。

新增 7 个业务场景与 12 个基础用例；全部使用假 HTTP 和临时文件，实际模型 token 数与效果不在验收范围内。2026-10-28 全量 **226 passed in 36.77s**；Ruff 格式、lint 与 Pyright 通过。

V3.2 验收（2026-10-28）：替换旧的“历史超限直接失败”场景，新增整轮选择与边界场景，全量 **229 passed in 37.34s**；Ruff、Pyright 通过。

V3.3 验收（2026-10-28）：新增 18 个用例，全量 **247 passed in 39.92s**；摘要请求中、写入中、提交后的真实强杀与恢复通过，Ruff、Pyright 通过。

## 怎样运行

在项目根目录使用项目虚拟环境：

```powershell
# 查看业务测试任务，不执行
.\.venv\Scripts\python.exe -m pytest tests/features --collect-only -q

# 执行全部业务功能验收
.\.venv\Scripts\python.exe -m pytest tests/features -v

# 只验收失败轨迹
.\.venv\Scripts\python.exe -m pytest tests/features/test_execution_trace.py -v

# 全量回归，包含基础校验
.\.venv\Scripts\python.exe -m pytest -q
```

pytest 已配置扫描整个 tests；现有 pre-commit 的 pytest 钩子会自动收集这个目录，无需额外配置或手工注册。

## 模拟范围与完成含义

模型 HTTP 响应是预设的，测试不会消耗 API 额度；文件、JSONL、锁和部分子进程会在临时目录实际运行。故障测试在指定边界注入错误，验证程序停止或保留记录的行为。

这些场景不能证明真实模型能正确决定是否使用工具，也不代替用户对配色的辨识体验。真实进程强杀已由专门测试覆盖；V2.3 按用户决定暂跳过；V3.4 自动压缩、文件编辑与流式取消等未实现功能的验收仍在 [练手 TODO](../../docs/agent-practice-todo.md)；后续补测规则见 [AGENTS.md](../../AGENTS.md)。
