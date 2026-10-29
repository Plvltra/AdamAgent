# 业务功能测试任务

更新日期：2026-10-29

这里按用户可见的能力组织自动化验收。功能模块改名后，仍能按“会话恢复”“命令执行”等业务名称找到对应测试。每个文件包含可独立运行的 pytest 场景；参数化场景展开为多个用例。

## 已实现功能与测试入口

| 功能点 | 要验收的业务结果 | 测试文件 |
| --- | --- | --- |
| E4.3 自动反思与纠正 | 同批完成后触发、次数上限、降级、停止、重启关联和历史隔离 | [test_reflection_loop.py](test_reflection_loop.py) |
| E4.2 反思请求 | 无工具请求、可靠结算、格式降级、跳过预算和保存失败 | [test_reflection_request.py](test_reflection_request.py)、[配置](../test_reflection_config.py) |
| E4.1 反馈记录 | 真实来源、原文引用、重复/伪造拒绝、失败输入隔离和重启不重跑 | [test_reflection_feedback.py](test_reflection_feedback.py) |
| E3.4 计划整体验收 | 同一两文件任务的有/无计划开销、真实编辑与命令证据、子结果成功/失败、简单问答、文档检索作为计划证据 | [test_task_plan_acceptance.py](test_task_plan_acceptance.py) |
| E3.3 计划交互 | /plan 只读与重启、一次提醒及额度、有限调整、流式收尾和展示失败 | [test_task_plan_interaction.py](test_task_plan_interaction.py) |
| E3.2 模型计划与证据 | 真实读文件与命令记录、伪造/跨轮/失败证据拒绝、保存失败停止及配置 | [test_task_planning.py](test_task_planning.py)、../test_planning_config.py |
| E3.1 计划存储 | 版本与来源、跨会话隔离、重启不重跑、非法状态/关联及 fsync 回滚 | [test_task_plan_storage.py](test_task_plan_storage.py) |
| E2.5 文档整体验收 | 真实子线程查询、权限撤销、取消、资源/向量保存失败及相对工作目录 | [test_document_acceptance.py](test_document_acceptance.py) |
| E2.4 语义检索与评估 | 向量复用/更新/模型变化、非法维度、禁止隐式下载及固定评估口径 | [test_document_embeddings.py](test_document_embeddings.py)、[test_document_evaluation.py](test_document_evaluation.py) |
| E2.3 文档工具闭环 | 假 HTTP + 真实检索、来源和日志、按需执行、预算及配置 | [test_document_tools.py](test_document_tools.py) |
| E2.2 文档索引 | 同时间戳更新、删除、重启、真实双进程、锁竞争、损坏与保存失败 | [test_document_index.py](test_document_index.py) |
| E2.1 文档检索基线 | 固定文档和问题、真实关键词检索、片段来源及查询边界 | [test_document_retrieval.py](test_document_retrieval.py) |
| E1.5 记忆整体验收 | 固定全部/相关输入比较、时间跨期与应用重启、额外读取权限不扩范围、锁竞争/fsync 故障及禁用模式 | [test_memory_acceptance.py](test_memory_acceptance.py) |
| E1.4 记忆继承与故障 | 子线程固定快照、续聊取新版本、共享库直接访问被拒绝、损坏/权限降级、会话错误仍停止 | [test_memory_boundaries.py](test_memory_boundaries.py) |
| E1.3 记忆检索与输入 | 新会话召回、修改删除生效、范围与过期过滤、同轮固定、预览无请求和请求版本留痕 | [test_memory_context.py](test_memory_context.py) |
| E1.2 记忆管理 | /memory 生命周期、来源、项目与全局范围、过期、版本冲突、正文保真和写入失败反馈 | [test_memory_management.py](test_memory_management.py) |
| E1.1 记忆存储 | 跨会话独立存储、真实多进程写入、版本冲突、范围隔离及损坏/写入失败保留 | [test_memory_storage.py](test_memory_storage.py) |
| V6.3 中断与边界 | 真实 HTTP 单独/整组取消、另一终端隔离、强杀后恢复已提交回答、索引失败及资源限额 | [test_subagent_recovery.py](test_subagent_recovery.py)、[test_subagent_boundaries.py](test_subagent_boundaries.py) |
| V6.2 子 Agent 委派 | 两子任务实际重叠、读取文件、父子关联、重启追问、权限拒绝、提前回答拦截与共享额度 | [test_subagents.py](test_subagents.py) |
| V5.5 安装与迁移 | 干净环境安装及回归、停止写入后备份 JSONL，恢复到新目录仍能追加，原目录不变 | [test_backup_restore.py](test_backup_restore.py)、[交付指南](../../docs/install-and-recovery.md) |
| V5.4 失败后继续 | 重开会话的可靠/未知消费、消费限额、未知命令和编辑拦截聊天/摘要、CLI 继续方式与失败输入隔离 | [test_failure_recovery.py](test_failure_recovery.py) |
| V5.3 有限重试 | 连接失败独立记账、请求预算、摘要重试、非重试错误、日志失败与命令不重跑 | [test_request_retry.py](test_request_retry.py) |
| V5.2 取消本轮 | 独立 CLI 进程、真实 HTTP 等待、父子命令清理、确认取消、返回输入及用量保留 | [test_cancellation.py](test_cancellation.py) |
| V5.1 流式输出 | UTF-8/网络任意分片、隐藏推理、工具收齐后执行、断流不保存、不自动切模式、配置校验 | [test_streaming.py](test_streaming.py) |
| V4.2b 执行事实报告 | 流式与非流式 CLI 正常/失败/拒绝/超时/未知结果、读取失败、回答保存失败；不采信模型自述、不污染历史 | [test_execution_report.py](test_execution_report.py)、[test_repair_loop.py](test_repair_loop.py)、[test_process_crash.py](test_process_crash.py) |
| V4.2a 有限修正 | 真实检查失败后编辑重测、拒绝停止、同批/跨请求计数及新轮重置 | [test_repair_loop.py](test_repair_loop.py) |
| V4.1c 模型编辑与恢复 | 真实工具闭环、拒绝停止、日志失败与伪造关联拒绝、四个强杀边界 | [test_edit_tool_loop.py](test_edit_tool_loop.py)、[test_process_crash.py](test_process_crash.py) |
| V4.1a/b 文件修改 | 版本、唯一匹配、字节保真、diff 批准、独立权限、外部冲突及写入失败 | [test_file_editing.py](test_file_editing.py) |
| 多轮聊天与模型切换 | 历史正确传递，新会话隔离；切换模型保留历史；终端指令不作为用户问题发给模型 | [test_conversation.py](test_conversation.py) |
| 配置启动 | 两份 TOML 与命令行覆盖可用；主配置或模型配置错误时在创建 HTTP 客户端前停止 | [test_startup_configuration.py](test_startup_configuration.py)、[test_session_lifecycle.py](test_session_lifecycle.py) |
| 会话保存与恢复 | 跨启动恢复、不串话、失败切换不丢当前会话；回答已生成但保存失败时仍可见且不重试 | [test_session_lifecycle.py](test_session_lifecycle.py) |
| 工具调用与预算 | 模型请求工具、执行后回传、再回答；工具错误可修正；单批额度不足不执行；同轮 ID 唯一、跨轮可复用 | [test_tool_loop.py](test_tool_loop.py) |
| 通用命令 | 确认后才产生副作用，拒绝不执行；退出码、超时、输出限制、环境变量隔离与执行日志符合契约 | [test_command_execution.py](test_command_execution.py) |
| 终端角色与按需用量 | 默认不刷 token，/usage 不请求模型且跟随当前会话；颜色只影响前缀，重定向退回纯文本 | [test_terminal_interaction.py](test_terminal_interaction.py) |
| V3.4b 自动摘要 | 自动触发、素材缩减、每轮一次、请求/消费共用限额、失败停止及受保护输入拦截 | [test_auto_compaction.py](test_auto_compaction.py) |
| V3.4b 固定长对话对比 | 全量/选择/摘要的约束可见性、消息估算和模拟消费；不代表真实模型质量 | [test_context_evaluation.py](test_context_evaluation.py) |
| V3.4a 旧工具结果缩减 | 真实文件工具结果仅在请求中缩减、来源与日志保留，无额外模型请求 | [test_tool_result_pruning.py](test_tool_result_pruning.py)；基础边界见 `../test_context_pruning.py` |
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
| 应用边界与资源 | 不暴露模型配置也可聊天；注册新工具仍可留痕；错误信息和未保存回答不丢失；退出及失败释放资源 | [test_application_boundary.py](test_application_boundary.py) |

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

V3.4a 验收（2026-10-28）：新增 11 个用例，全量 **258 passed in 41.60s**；覆盖压力/开关、近期保护、状态保留、共享列表与真实工具链，Ruff、Pyright 通过。

V3.4b 验收（2026-10-28）：新增 14 个用例，全量 **272 passed in 49.41s**；Ruff、Pyright 通过。固定假模型对比结果及统计口径见 [上下文管理](../../docs/context-management.md)。

## V6.2 委派验收

- [x] 两个子 Agent 的 HTTP 请求同时进入，主 Agent 仍能请求；各自执行真实文件工具。
- [x] 子上下文继承系统约束但不复制主会话用户历史，工具目录不能再次委派。
- [x] 修改和通用命令不申请批准、不产生副作用，返回限制给主 Agent。
- [x] 独立保存子会话及父轮次/请求/工具关联，重启不发请求，可显式续聊。
- [x] 共享用量包含全部子请求；未知消费跨重启仍阻止请求；预留不足不登记假请求。
- [x] 并发和新建数量分别限制，主模型未收集结果时不能提交成功回答。
- [x] V6.3：真实 HTTP 单独取消与整组 Ctrl+C、独立进程强杀和恢复；另一终端继续运行。

V6.1 基础的并发准入、取消信号与配置校验位于 ../test_subagent_budget.py。
V6.3 已用真实本地 TCP 和独立进程补齐中断验收；已完整提交的子回答在索引收尾前强杀，
重启仍恢复原结果；未结算消费保持未知，不发新请求。另验证摘要/连接重试共用账本、
索引替换失败无子请求、索引缺失不能清零、读取授权、结果 JSON 长度与会话预览隔离。
同轮追问不能跳过上一结果的交付。V6 共 20 个业务场景，基础用例另有 9 个；不证明真实模型拆分任务的效果。

## E2/E3 集成核验（2026-10-29）

保留两项原有场景，新增真实文档检索 → 计划完成证据 → 重启查询的业务测试。`test_task_plan_acceptance.py` 共 5 项通过，合计收集 579 项；提交检查执行 Ruff 格式/lint、Pyright 与全量 pytest。工具注册、两组配置和持久化关联均保留。

## E2 本地文档检索验收

六个 test_document_*.py 文件共 44 项通过。检索与文件操作真实执行；HTTP 模型响应只替代远端，EmbeddingBackend 替身只验证向量协议与缓存。真实 FastEmbed 0.9.1 / BGE 中文小模型另外运行固定 16 题，报告见 [检索对照](../../docs/document-retrieval-evaluation.md)。无 embedding 可选依赖的 Python 3.11 新环境已验证普通启动、关键词查询及语义缺依赖提示。全量回归为提交前门槛，本阶段收集 546 项。

新增取消回调覆盖扫描及向量边界，实际文件 IO 和单次 ONNX 推理为协作取消；不宣称强制中止本地系统调用。坏缓存、撤销的权限、写入失败不得被当作无答案。最终回答的真实模型忠实性单列人工验证。

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

tests/test_tools.py 在测试进程内隔离提交钩子继承的 GIT_* 环境变量，临时仓库的提交、暂存与配置不得落到真实仓库。

pytest 已配置扫描整个 tests；现有 pre-commit 的 pytest 钩子会自动收集这个目录，无需额外配置或手工注册。

## 模拟范围与完成含义

模型 HTTP 响应是预设的，测试不会消耗 API 额度；文件、JSONL、锁和部分子进程会在临时目录实际运行。故障测试在指定边界注入错误，验证程序停止或保留记录的行为。

这些场景不能证明真实模型能正确决定是否使用工具，也不代替用户对配色的辨识体验。真实进程强杀已由专门测试覆盖；V2.3 按用户决定暂跳过；V3 的压缩流程已离线验收；真实模型及个人理解验收的剩余项见 [练手 TODO](../../docs/agent-practice-todo.md)；后续补测规则见 [AGENTS.md](../../AGENTS.md)。

## V5 前置集成核验（2026-10-29）

将应用边界重构重放到 V4.1b：299 个主线用例收集项全部保留，增加 15 个架构与应用边界用例，全量 314 passed；Ruff 和 Pyright 通过。仅迁移测试的依赖组装，原业务断言保留。

V5.1 定向验收：17 个新场景通过；原非流式场景显式使用 stream=false，保持原断言。

V5.1 全量回归：331 passed；Ruff、格式检查、Pyright 通过。

V5.5 干净环境验收：Python 3.12.14、按 uv.lock 新建环境；CLI --help、Ruff、Pyright 通过，357 passed in 82.01s。真实 API 与物理键盘人工清单见交付指南。

## 职责包整理验收（2026-10-29）

业务测试仍按能力归档，模块迁移只调整导入及故障替身的目标路径。原 408 项收集记录全部保留，新增 8 项整包依赖约束，共 416 项；原五项架构边界保留场景 ID。边界检查位于 [test_architecture.py](../test_architecture.py)，将来添加的源码也会被扫描。提交前执行 Ruff 格式与 lint、Pyright 和全量 pytest，模型使用既有假响应，不消耗 API 额度。

## V4 与 V5 集成验收（2026-10-29）

保留 Adam2 的 357 个场景与主线 V4 后续增加的 36 个场景，初次收集 393 项；再增加 14 个流式事实报告场景和 1 个未知编辑结果拦截场景，共 **408 passed in 105.61s**，Ruff、Pyright 通过。原 V4 修复/拒绝、编辑强杀恢复、编辑额度与结果报告断言保留，仅迁移组装入口；报告与失败继续方式均通过 AgentApplication 提供，架构边界测试保持。
