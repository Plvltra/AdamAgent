# 业务功能测试任务

更新日期：2026-11-02

这里按用户可见的能力组织自动化验收。功能模块改名后，仍能按“会话恢复”“命令执行”等业务名称找到对应测试。每个文件包含可独立运行的 pytest 场景；参数化场景展开为多个用例。

## 已实现功能与测试入口

| 功能点 | 要验收的业务结果 | 测试文件 |
| --- | --- | --- |
| B32.1 首错报告 | 真实工具失败定位、成功后不误归因、工具成功但答案错误 | [首错验收](test_first_error.py) |
| B31.3 消融报告 | 真实记忆选择开关、正常反例、缺失分母与配对 | [对照验收](test_task_eval_report.py) |
| C01.4 真实五题评估器 | 新增 9 项：实际本地重定向/来源、两组普通问题零网络、未知用量停止、HTTP 失败不重试、固定批次/端点拒绝及引用范围不冒充语义；真实两组严格完整通过 0/5、2/5，各 1 项部分通过 | [test_web_evaluation.py](test_web_evaluation.py)、[真实记录](../../docs/evaluations/c01-web-comparison.json)、[限制](../../docs/web-reading.md#固定五题真实对照c014) |
| B31.2 评估矩阵 | 独立多配置/重复、错误答案分母、未知用量停止、显式live | [矩阵验收](test_task_eval_runner.py) |
| B31.1 固定任务 | 独立重置、实际文件终态、记忆来源、普通问题不调用工具 | [资产验收](test_task_eval_assets.py) |
| B30.3 真实shell与强杀 | 连续cwd/env、双shell隔离、非零退出、输入未知不重发 | [真实验收](test_terminal_acceptance.py) |
| B30.2.2 终端聊天 | 真实进程与CLI批准、输入拒绝、关闭和来源关联 | [聊天验收](test_terminal_chat.py) |
| B30.2.1 终端持久 | 输入来源、关闭恢复不重发、保存失败阻止启动 | [持久验收](test_terminal_persistence.py) |
| B30.1 持久终端 | 真实双进程cwd/env隔离、逐次拒绝、跨会话拒绝与单独取消 | [终端验收](test_terminal_sessions.py) |
| B29.3 条件故障验收 | 真实命令输出合并、退出码、取消、两次写入间强杀不重发 | [故障验收](test_trigger_acceptance.py) |
| B29.2.2 聊天条件 | 模拟HTTP工具闭环、来源记录、恢复不重发和默认模式隔离 | [聊天条件](test_trigger_chat.py) |
| B29.2.1 条件协调 | 一次性合并、队列背压、跨轮次隔离 | [条件验收](test_local_triggers.py) |
| B29.1 本地定时 | 到期/取消、独占、恢复只读及保存失败 | [定时验收](test_local_timers.py) |
| B28.3 消息故障验收 | 双子任务隔离、取消后拒绝补充、两个真实强杀边界恢复不重发 | [消息验收](test_subagent_message_acceptance.py) |
| B28.2 双向持久消息 | 子进展报告/父收集、入站来源、持久状态、保存失败及恢复不重发 | [双向消息](test_subagent_progress.py) |
| B28.1 运行中父子消息 | 请求中补充、message_id去重、旧run拒绝及原续聊 | [父子消息](test_subagent_live_messages.py) |
| B27.3 输入重启恢复 | 三个真实强杀边界、成功历史判定、不重发、工具补充及乱序拒绝 | [恢复验收](test_running_input_recovery.py) |
| C/B15.3 媒体目录交集 | 新增 5 项：主图片拒绝 MCP 保留目录、真实子任务在规则开/关两种配置下拒绝本地/MCP 保留媒体；不发布正文或来源，原字节不变 | [test_image_input.py](test_image_input.py) |
| C00.4 独立单位与估价 | 新增 105 项：50 单位/并发、16 配置、13 存储/估价、24 实际主子/反思/摘要/重放及 2 显式图问；未知/取消/保存/旧日志/限额/价格冻结/收尾竞争，提交门禁运行全量检查；音频消费者仍待 C05 | [实际业务](test_consumption_loop.py)、[存储](test_consumption_evidence.py)、[单位](../test_consumption.py)、[配置](../test_consumption_config.py)、[图问](test_image_questions.py)、[限制](../../docs/multimodal-input.md#c004-多单位消费) |
| B27.2.2 终端运行输入 | 输入泵投递、停止/恢复、独立留痕、保存失败及真实命令取消 | [终端输入](test_live_input.py) |
| B27.2.1 输入安全点 | 请求/工具批次边界消费、成功历史、最终封口和配对约束 | [安全点验收](test_running_input_boundary.py) |
| B27.1 运行事件队列 | 有界FIFO、去重/冲突、关闭保留、并发投递 | [队列契约](test_running_events.py) |
| B26.3 真实后台故障 | 双进程输出隔离、独立取消、强杀宿主后恢复未知且不重跑 | [实际验收](test_background_acceptance.py) |
| B26.2.2 生产后台工具 | 模型启动/等待真实进程、Ctrl+C、任务锁/恢复、意图保存失败无执行 | [持久任务](test_background_persistence.py) |
| B26.2.1 输出游标 | 退出前短输出可读、独立续读、缺口与字符边界、会话所有权 | [输出游标](test_background_output_cursor.py) |
| B26.1 后台进程 | 真实返回、一次批准、所有权、输出限额、取消及超时 | [后台任务](test_background_jobs.py) |
| B25.3 真实HTTP | 4项实际回环TCP/时钟验收心跳、慢输出、断流和总时限；相关123项通过 | [网络验收](test_stream_http_deadlines.py) |
| B25.2 流式期限接入 | 5项心跳超时/关闭、有效慢输出、已知与未知用量及真实会话恢复；相关119项通过 | [流式期限](test_stream_deadlines.py) |
| B25.1 流式计时基础 | 8项心跳不续期、有效增量、总时限及TOML；相关94项通过，HTTP接入待B25.2 | [计时验收](test_stream_watch.py) |
| B24.3 协议恢复 | 3项实际历史切换不重跑、取消/超时未知用量跨重启保留；相关124项通过 | [恢复验收](test_model_protocol_recovery.py) |
| B24.2 双协议接入 | 11项用量、转换、TOML/HTTP、真实文件工具/恢复、拒绝私有字段与HTTP失败不重试；相关121项通过 | [Messages验收](test_messages_adapter.py) |
| B24.1 公共协议视图 | 6项工具ID关联、原推理/扩展保留、独立副本、拒绝不支持内容和能力声明；连同架构28项通过 | [协议视图](test_model_protocol_view.py) |
| B23.3 三百行对照 | 4项真实双路编辑/参数与结果证据、已有目录不覆盖、失败不判成功、生产远距离读取/编辑/撤销；相关84项通过，外部变化/边界沿用B23.1/2 | [组合验收](test_span_edit_acceptance.py)、[实际入口](../../scripts/span_edit_eval.py) |
| B23.2 首尾编辑工具 | 9项发现/真实编辑/检查点/撤销/重放、模式/拒绝/外部版本、真实Hook、编辑额度和保存失败；原子任务场景补新入口，相关133项通过 | [生产流程](test_span_edit_loop.py) |
| B23.1 首尾锚点规划 | 18项实际文件的包含范围、LF/CRLF/BOM、空候选、歧义/重叠/逆序、版本与权限/限额；相关96项通过，模型入口见B23.2 | [规划场景](test_span_edit_plan.py) |
| B22.3.2 创建故障验收 | 6项真实独立宿主：同名竞争、发布前后/结果保存后/撤销删除前后强杀，重启幂等且零请求/批准/写入；相关113项通过 | [恢复测试](test_creation_recovery.py)、[独立宿主](support/file_creation_worker.py) |
| B22.3.1 创建工具闭环 | 10项按需发现/同批拒绝、真实创建读取修改及两次撤销、模式/权限/参数/拒绝、两个真实语法Hook与目录限制、子Agent及零副作用重放；相关112项通过 | [生产创建](test_file_creation_loop.py) |
| B22.2.3.2 撤销创建 | 10项成功/空文件删除、批准/拒绝/取消、外部版本/撤权、删除与保存失败、unknown重启及不重复撤销；相关114项通过 | [创建撤销](test_creation_undo.py) |
| B22.2.3.1 创建留痕 | 9项真实执行器/文件/JSONL、空内容、拒绝、四处保存失败/unknown恢复、同名冲突与编辑额度；相关79项通过；生产目录在B22.3.1开放并默认拒绝未批准写入 | [创建记录](test_creation_records.py) |
| B22.2.2 原子创建API | 12项真实完整发布/同名文件与目录竞争、写入/flush/fsync/link故障/取消、清理失败；相关108项通过，日志和模型接入待后续 | [原子创建](test_atomic_file_creation.py) |
| B22.2.1 创建检查点基础 | 17项真实保存/重启、缺失与空文件/v1分离、格式损坏/发布失败、纯日志unknown与来源拒绝；相关106项通过，尚无实际创建/删除 | [创建检查点](test_creation_checkpoints.py) |
| B22.1.2 创建批准 | 12项完整候选展示、默认拒绝/EOF/取消、同名出现/父目录删除/撤权；相关105项通过，批准不代表写入 | [创建批准](test_file_creation_approval.py) |
| B22.1.1 新文件计划 | 15项无副作用规划、空文件/缺失区分、UTF-8/大小、已有目标/父目录、独立写权限及Windows别名；相关84项通过，批准/写入待后续 | [创建计划](test_file_creation_plan.py) |
| B21.3 搜索组合验收 | 真实目录改变→旧游标失败→模型显式重查；正则匹配中取消、保留已知用量及幂等恢复；2项新增、相关121项通过 | [完整应用场景](test_search_acceptance.py) |
| B21.2.2 显式正则搜索 | 9项真实工具/配置：默认字面无进程、模式游标、语法/大小/超时、模型翻页和禁止进程的离线重放；相关122项通过 | [正则搜索业务](test_regex_search.py) |
| B21.2.1 正则进程基础 | 17项真实逐行匹配/编码/参数限制、灾难回溯超时及已接收请求后取消；连同架构39项通过 | [固定进程场景](test_regex_worker.py) |
| B21.1.2 搜索分页 | 14项新增：61条完整续读、长度/版本/权限/总字节边界、模型第51条与零扫描重放；相关160项通过 | [实际工具与模型场景](test_file_query_pages.py) |
| B21.1.1 文件查询基础 | 真实稳定排序、权限、来源变化、扫描限制与取消；18项新增、相关40项通过，尚未改变聊天工具 | [扫描场景](test_query_inventory.py)、[游标校验](../test_file_query_cursor.py) |
| B20.3 诊断组合故障 | 5项真实输出超限、检查取消、日志fsync失败及幂等恢复；相关148项通过，成功修复沿用B20.2 | [故障场景](test_diagnostic_faults.py)、[修复闭环](test_diagnostic_request.py) |
| B20.2.2 诊断请求与重放 | 11项HTTP/实际编辑/日志/来源/预算/旧录制及2项契约新增；相关153项通过 | [业务场景](test_diagnostic_request.py)、[上下文边界](../test_diagnostic_context.py) |
| B20.2.1 诊断选择与版本观察 | 7项真实编辑/版本/权限/容量/故障与16项契约；相关100项通过，模型接入待下一步 | [业务场景](test_diagnostic_selection.py)、[契约](../test_diagnostic_context.py) |
| B20.1 编辑诊断协议 | 6项实际编辑/检查、版本变化、超限、来源伪造及重启，22项协议边界；相关126项 | [业务场景](test_edit_diagnostics.py)、[契约边界](../test_diagnostics.py) |
| B19.3.2 实际执行中断与恢复 | 真实取消/强杀各确认4个 Linux 身份回收，命令未知与用量已知分别保留，重启幂等；相关89项回归 | [真实验收入口](../../scripts/isolated_command_fault_acceptance.py)、[实际报告](../../docs/evaluations/b19-command-faults.json) |
| B19.3.1 真实隔离边界与输出编码 | 5项新增协议/完整性/重放，相关171项；18项真实边界、执行限额继承及4项真实编码另存证据 | [完整性](test_isolated_output_integrity.py)、[真实入口](../../scripts/isolated_boundary_acceptance.py) |
| B19.2.3 隔离命令接入 | 33项发现/批准/版本/权限/模式/Hook/预算/日志/计划/反思/重放，包含512字符失败停止；四项真实 WSL 聊天证据另存 | [业务场景](test_isolated_command_flow.py)、[真实入口](../../scripts/isolated_chat_acceptance.py) |
| B19.2.2 授权输入快照 | 25项授权/变更/限额/路径/协议，相关117项通过；实际只读挂载和2MiB原字节摘要已验收 | [业务场景](test_command_input_snapshots.py)、[真实入口](../../scripts/isolated_input_acceptance.py) |
| B19.2.1 原生命令隔离 | 21项参数/真实管道/故障/取消/无降级，相关75项通过；11项真实 WSL 命令证据另存，模型工具尚未接入 | [业务场景](test_isolated_commands.py)、[真实入口](../../scripts/isolated_command_acceptance.py) |
| B18.3 长输出故障 | 3项独立宿主SIGINT/期限/进程树、重启及超额零副作用重放；相关71和26项通过 | [test_command_output_acceptance.py](test_command_output_acceptance.py)、[独立宿主](support/command_output_worker.py) |
| B18.2 最终头尾预览 | 7项双流/附件配额/失败/字符预算，相关152项通过；停止反馈也不再只取前缀 | [test_command_output_preview.py](test_command_output_preview.py)、[重复失败](test_no_progress_loop.py) |
| B18.1 命令头尾捕获 | 19项末尾真实报错、字节数量、UTF-8、未知结尾与快照；相关73项通过，最终预览待B18.2 | [test_command_output_capture.py](test_command_output_capture.py) |
| B17.3.2 PDF 组合验收 | 5项真实文件/入口/配置场景，相关180项通过；313页原书索引与资源拒绝证据另存，未验证视觉或真实模型引用 | [test_pdf_acceptance.py](test_pdf_acceptance.py)、[真实入口](../../scripts/pdf_acceptance.py) |
| B17.3.1 图片扩展 API | 24项来源/单次调用/不可用/用量/故障契约，相关97项通过；无生产视觉后端，不代表视觉效果验收 | [test_image_analysis_contract.py](test_image_analysis_contract.py) |
| B17.2.2 PDF 页级索引 | 21项新增；两组相关143与227项通过，涵盖来源/扫描提示/版本/权限/故障/重放；主线门禁全量验证 | [test_pdf_indexing.py](test_pdf_indexing.py) |
| B17.2.1 PDF 读取工具 | 15项权限/变化/页码/额度/取消/配置/重放；两组相关162和111项通过，新增TOML后62项 | [test_pdf_tools.py](test_pdf_tools.py) |
| C04.2 显式本地视觉问答 | 62 项业务/配置/复现边界：原字节/来源与原系统约束、无旧历史/工具、JSON/SSE、未知费用、主子共享额度、超时/取消/保存失败、真实 TCP/CLI/重启及不重复结算；真实固定烟测首批 2/4、复验 3/4，失败保留 | [test_image_questions.py](test_image_questions.py)、[真实入口](../../scripts/vision_acceptance.py)、[限制与证据](../../docs/visual-question-answering.md) |
| C04.1 本地图片输入 | 29 项业务、36 项解码及 13 项配置/独立进程导入：真实 PNG/JPEG/WebP、尺寸/EXIF 方向、原字节与版本、显式附图/无能力拒绝、发现/规则/Hook/预算、取消/保存失败、重启/父子隔离及零解码事实重放；C04.2 已另做有限真实烟测，综合视觉/OCR 质量待 C04.3～4 | [test_image_input.py](test_image_input.py)、[解码](../test_image_decoding.py)、[配置](../test_vision_config.py) |
| B17.1 PDF 提取基础 | 15项真实页码/版本/图像标记/截断/加密/解码限额/取消/配置隔离；架构合计37项，真实中文书稿三页另存统计 | [test_pdf_extraction.py](test_pdf_extraction.py) |
| B16.3 二十工具对照 | 7项报告、错误与权限验收；相关共115项；真实本地命中9/10，多余候选与失败原样归档 | [test_tool_discovery_evaluation.py](test_tool_discovery_evaluation.py) |
| B16.2 语义发现接入 | 8项真实发现/请求名单/文件调用、同批与路径拒绝、降级/分页、变更目录、主子隔离及重启重放；相关共108项 | [test_semantic_tool_flow.py](test_semantic_tool_flow.py) |
| B16.1 语义工具目录基础 | 22项真实目录权限/选择隔离、固定向量排序、缓存失效、坏批次/取消和配置；相关共136项，聊天接入待B16.2 | [test_semantic_tool_directory.py](test_semantic_tool_directory.py) |
| B15.3.3 MCP 跨传输组合 | 10项真实认证JSON/SSE的三类媒体、原工具、坏正文/满缓存、取消/重启/零网络重放；与远程和stdio共62项 | [test_mcp_acceptance.py](test_mcp_acceptance.py) |
| B15.3.2.2 MCP 媒体接入 | 23项真实SDK混合内容、来源/版本、整份拒绝与保存失败、主子保护、重启及离线重放；相关共167项 | [test_mcp_media_results.py](test_mcp_media_results.py) |
| B15.3.2.1 二进制引用存储 | 23项真实保存/重启/来源、格式与额度、残留/损坏/写入故障和独立进程竞争；连同架构共45项，聊天接入待下一步 | [test_mcp_media_storage.py](test_mcp_media_storage.py) |
| B15.3.1 MCP HTTP | 29项真实回环JSON/SSE、认证不落盘、固定端点、TLS/代理、故障/取消和零网络重放；原103项相关回归通过 | [test_mcp_remote.py](test_mcp_remote.py) |
| B15.2 MCP 提示模板 | 31项真实SDK目录/参数、角色与来源、限额、取消/断开、冲突、教学服务及零服务重放；相关共103项 | [test_mcp_prompts.py](test_mcp_prompts.py) |
| B15.1 MCP 资源 | 26 项真实 SDK 分页、URI、版本、限额、取消/故障与教学示例；模型工具循环及零服务重放，相关共 72 项 | [test_mcp_resources.py](test_mcp_resources.py) |
| B14.3 编排故障与清理 | 8 项真实管道成功/失败/取消与回调异常，相关共 112 项；六类真实 WSL 故障与强杀报告另存 | [宿主清理](test_orchestration_cleanup.py)、[真实故障入口](../../scripts/orchestration_fault_acceptance.py) |
| B14.2.2 生产隔离工具 | 17 项发现/关闭/参数、失败重放、配置、启动竞争、子任务和计划底层证据；相关共 176 项，真实 WSL 聊天报告独立保存 | [业务场景](test_code_orchestration_loop.py)、[真实入口](../../scripts/chat_orchestration_acceptance.py) |
| B14.2.1 嵌套执行 | 24 项父子来源、原权限/批准/Hook、共享额度、取消/保存失败、线程/递归限制与离线重放；相关共 175 项 | [业务场景](test_nested_tool_execution.py)、[来源边界](../test_nested_tool_log.py) |
| B14.1 窄 RPC | 17 项协议/真实工具桥与相关共 63 项；另有实际 WSL 双工具筛选、拒绝、直接访问及回复阻塞停止证据，聊天接入待 B14.2 | [契约场景](test_code_orchestration.py)、[真实入口](../../scripts/orchestration_acceptance.py) |
| B19.1 隔离前置 | 24 项宿主协议与架构共 46 项；独立真实 12 探针与 8 执行案例，不能以替身代替 | [宿主契约](test_isolation_backend.py)、[真实入口](../../scripts/isolation_acceptance.py)、[证据](../../docs/runtime-isolation.md) |
| B13.3 分享组合验收 | 17 项真实工具/回复与转义 JSON、完整长凭据、重复/非有限/深度/大小、无标签和编码漏报、技术字段误报、显式规则及旧版读取；相关共 55 项 | [test_sharing_acceptance.py](test_sharing_acceptance.py) |
| B13.2 分享命令与来源 | 22 项离线 CLI/私有规则、来源匹配与变化、不可重放、格式/元数据/复杂度、错误不回显原文；相关共 70 项 | [test_sharing_commands.py](test_sharing_commands.py) |
| B13.1 分享脱敏 API | 16 项真实读取/回复录制、重复秘密、位置报告、指定地址、同名/硬链接拒绝、写入失败、残缺前缀和资源限制；相关共 70 项 | [test_recording_sharing.py](test_recording_sharing.py) |
| B12.3 知识故障验收 | 15 项：独立进程竞争 4 项、请求/审查/发布/撤回/来源失效强杀 10 个边界、冲突知识独立决定；重启零自动请求 | [test_knowledge_recovery.py](test_knowledge_recovery.py)、[进程入口](support/knowledge_worker.py) |
| B12.2.3 知识使用入口 | 23 项候选原文补全、发布查询与分页、完整反证、真实聊天/子任务/重放、原始目录保护、配置和 CLI 确认；相关共 66 项 | [test_knowledge_entries.py](test_knowledge_entries.py) |
| B12.2.2 独立模型审查 | 24 项 JSON/SSE、请求先落盘、无工具/无批准、用量未知/超额/取消、保存失败、旧来源拒用和重复 ID；相关共 72 项 | [test_knowledge_review.py](test_knowledge_review.py) |
| B12.2.1 知识发布与撤回 | 27 项明确确认、锁外回调、过时审查/确认拒绝、有效期、源变化/撤权、原子保存故障及损坏历史；相关共 66 项 | [test_knowledge_publication.py](test_knowledge_publication.py) |
| B12.1 知识候选 | 18 项真实冲突来源、时间/引用、冻结重启、范围隔离、保存/撤权/取消、ID 碰撞与伪造发布；相关共 60 项 | [test_knowledge_candidates.py](test_knowledge_candidates.py) |
| B11.3 总结与原文依据 | 21 项来源审计/缺失过时错绑/撤权/先行校验、固定对照、两路模型工具闭环与归档；相关共 76 项 | [test_knowledge_acceptance.py](test_knowledge_acceptance.py) |
| B11.2 概览与原文查询 | 16 项分页与跨文件、来源变化/撤权/取消、输出边界、CLI、工具闭环和重放；相关共 55 项 | [test_knowledge_queries.py](test_knowledge_queries.py) |
| B11.1 二层知识目录 | 12 项真实索引、主题合并、全部引用、逐字截断、更新删除、重启损坏、保存失败和取消 | [test_knowledge_catalog.py](test_knowledge_catalog.py) |
| B10.3 语境对照与生成 | 19 项固定资料/JSON与SSE/预算与未知/捕获失败/取消/工具拒绝/错误说明和版本/显式入口；归档实际本地退步 | [test_context_prefix_evaluation.py](test_context_prefix_evaluation.py) |
| B10.2 语境检索接入 | 13 项三路检索与原文分离/前缀向量失效/源变化/关闭与故障/输出限额/日志重放/配置 CLI | [test_contextual_retrieval.py](test_contextual_retrieval.py) |
| B10.1 独立语境说明 | 11 项原文分离/来源与生成器变化/有限输入/缓存重启/失败原子保存/错绑与损坏拒绝 | [test_context_prefixes.py](test_context_prefixes.py) |
| B09.3 多证据评估 | 16 项金标准/不同分母/来源变化/重复与超限/四路对照/旧报告保留/实际证据留存；真实十二题结果独立归档 | [test_retrieval_metrics.py](test_retrieval_metrics.py)、[真实评估](../../docs/retrieval-evaluation.md) |
| B09.2 可选本地精排 | 18 项候选池/原依据、部分失败整体降级、有限前缀、取消/预算、关闭/空池、本地加载、缺少依赖、日志/重放和真实状态评估；相关共 146 项 | [test_document_reranking.py](test_document_reranking.py) |
| B09.1 文档混合召回 | 16 项候选池/排名/去重、独立单路、来源更新/删除、模型/缓存/取消故障、工具输出/重放、配置/CLI；相关共 128 项 | [test_hybrid_retrieval.py](test_hybrid_retrieval.py) |
| B08.3 来源变化组合验收 | 8 项同轮编辑/删除/损坏/拒读、真实追加竞争、压缩重启、诊断状态、撤销授权及实际目录链接；相关共 92 项 | [test_session_search_acceptance.py](test_session_search_acceptance.py) |
| B08.2 历史工具接入 | 压缩前原文/按需发现/续读、授权/诊断/切换、限额/关闭/plan/工具次数、真实 Hook/子任务隔离及离线重放 | [test_session_search_tools.py](test_session_search_tools.py) |
| B08.1 旧对话索引 | 15 项授权、精确来源、追加/编辑、失效缓存、诊断隔离、资源限额、分页、越界和只读 CLI | [test_session_search.py](test_session_search.py) |
| B07.3 拒绝与中断恢复 | 9 项明确拒绝/保存失败/到期、独立进程同时确认或编辑或拒绝、原子替换前后强杀重启；已接受结果保留可撤销 | [test_memory_organization_recovery.py](test_memory_organization_recovery.py) |
| B07.2 确认与撤销 | 15 项概览/来源/幂等确认、明确解决冲突、原子故障、并发保护、有效期、损坏及请求重放 | [test_memory_consolidation.py](test_memory_consolidation.py) |
| B07.1 离线整理候选 | 16 项四类候选、确切来源、范围和同名隔离、不调用模型、重启、损坏与保存失败；相关回归共 70 项 | [test_memory_organization.py](test_memory_organization.py) |
| B06.3 召回组合验收 | 7 项固定题集/来源留证、不可用与降级、写入停止、主子版本继承；真实本地十题结果另存并保留失败 | [test_memory_retrieval_acceptance.py](test_memory_retrieval_acceptance.py)、[真实对照](../../docs/memory-retrieval-evaluation.md) |
| B06.2 主体与事实时间 | 20 项同名/显式主体、有效期、原子替代、失败回滚、重启、错误来源及真实请求/重放；相关回归共 131 项 | [test_memory_identity.py](test_memory_identity.py) |
| B06.1 本地语义记忆 | 12 项范围/版本/预算/降级/取消/请求/重放业务，17 项配置与来源边界；相关回归共 117 项，真实召回质量待验收 | [业务场景](test_semantic_memory.py)、[契约边界](../test_semantic_memory.py) |
| C00.3 多模态消息与冻结回放 | 35 项业务和 53 项契约/归档，含 B13.1 仅解析/人工分享不得回放：实际 PNG 编码/工具推理/SSE/持久化、无能力/来源/限额/预算/保存/取消、摘要反思/真实子任务隔离、完整字节副本与原会话删除后零外部动作重放；真实视觉质量待 C04 | [test_multimodal_input.py](test_multimodal_input.py)、[公共边界](../test_model_input.py)、[归档边界](../test_media_archive.py) |
| C01.3 网页研究与片段引用 | 17 项真实 HTTP/主子/重启业务、28 项引用边界与 6 项新增配置：搜索→正文→逐字引用、原模式/批准、保存/取消/来源故障、隔离与零网络重放；真实模型有限对照已由 C01.4 单列，不能据此宣称可靠搜索 | [test_web_research.py](test_web_research.py)、[引用边界](../test_web_evidence.py)、[配置](../test_web_config.py) |
| C01.2 独立百科搜索 | 27 项真实本地 HTTP 业务、69 项协议/工厂边界：有界候选/空结果、来源版本、限流/配置/取消、整批校验、无重试/重定向/代理/cookies；真实百科后端两次留证，不证明通用搜索或模型质量 | [test_web_search.py](test_web_search.py)、[边界](../test_web_search.py)、[真实记录](../../docs/evaluations/c01-search-backend.json) |
| C02.3 浏览器生命周期 | 33 项真实本地业务与 17 项边界：标签/弹窗/对话框、批准关闭/下载、完整身份分页、来源/配额/提交故障、cookies 不入日志、取消、真实 POST 后终止/崩溃及重启不重提、零动作引用重放；真实模型质量待 C02.4 | [test_browser_lifecycle.py](test_browser_lifecycle.py)、[边界](../test_browser_lifecycle.py) |
| C00.2 二进制媒体证据 | 19 项业务、51 项低层边界：有效 PNG/WAV 完整读写、引用来源/所有者、JSONL 无正文、缺失/损坏/超限、跨会话、真实写入/提交故障、并发、备份与主子目录保护；多模态输入已由 C00.3 接入，真实解码见 C04.1 | [test_media_evidence.py](test_media_evidence.py)、[边界](../test_media_evidence.py) |
| C02.2 逐次批准浏览器动作 | 25 项主应用业务场景、40 项协议/配置/CLI 边界；真实填表/选择/滚动/POST/303 回执、原预检拒绝、引用/会话/批准期间替换、拒绝/取消、日志/SDK/OS/观察故障、重定向、零动作重放与主子隔离；真实模型质量未验收 | [test_browser_actions.py](test_browser_actions.py)、[边界](../test_browser_actions.py) |
| C02.1 DOM 浏览器观察 | 真实 Chromium/动态本地页、版本与控件时效、同名替代/属性变化、截断、逐跳站点拒绝、自动 POST/WebSocket 零请求、cookies 隔离、超时/取消/资源故障与 CLI；模型动作见 C02.2 | [test_browser_observation.py](test_browser_observation.py)、[配置/导入边界](../test_browser_policy.py) |
| C00.1 可选能力与资源 | 默认禁用/无后端导入、主子发现读取、真实 HTTP 独立关闭、预算/Hook 拒绝、取消与创建/清理失败、完整来源限长、零网络离线重放；21 个原网页场景迁移无变化 | [test_optional_web.py](test_optional_web.py)、[配置边界](../test_web_config.py) |
| C01.1 网页抓取 | 真实本地 HTTP 正文/来源版本、逐跳及 DNS 重绑定策略、字节/字符上限、错误分类、超时与取消、代理/cookie 隔离及独立 CLI；搜索/模型质量未验收 | [test_web_reading.py](test_web_reading.py)、[策略边界](../test_web_policy.py) |
| B05.3 摘要对照与人工判读 | 28 项同历史双查询、JSON/SSE、预算与未知消费、故障停止、记录/评审来源校验及只读报告；真实效果待显式评估 | [test_compaction_evaluation.py](test_compaction_evaluation.py) |
| B05.2 压缩保真样本 | 27 项四类判据、原文位置/覆盖范围、否定/矛盾/遗漏不自动判真、伪造资料/引用及只读 CLI；语义质量待评审 | [test_compaction_fidelity.py](test_compaction_fidelity.py) |
| B05.1 任务导向摘要 | 9 项当前问题/真实工具前缀、原文与覆盖范围、失败/重启、预算、手动空任务、旧新重放及伪造来源拒绝场景；真实语义保真另行评估 | [test_task_compaction.py](test_task_compaction.py) |
| B04.3 状态组合验收 | 7 项真实主子在途预留/收集、父子输入与各自日志、失败隔离、伪造状态、硬限额、未知消费及禁用观测场景 | [test_runtime_state_acceptance.py](test_runtime_state_acceptance.py) |
| B04.2 模型状态输入 | 5 项业务、7 项数据边界：重试/工具后状态与日志、旧新重放、预览、读取失败、损坏与有界表示；相关回归共 208 项，后续全量回归修复相关 61 项；原权限和执行预算保持 | [请求场景](test_runtime_context.py)、[数据边界](../test_runtime_state.py) |
| B04.1 只读执行状态 API | 7 项业务场景：真实请求重试、共享预留与未知、旧快照无授权、失败重置与不可变汇总；11 项契约边界；聊天接入见 B04.2 | [业务状态](test_runtime_state.py)、[数据边界](../test_runtime_state.py) |
| B03.3 缓存/耗时对照 | 26项JSON/SSE、两布局、实际用量/价格/未知、失败/取消/保存故障、预算停止及只读报告；真实效果待显式运行 | [test_cache_evaluation.py](test_cache_evaluation.py) |
| B03.2 上下文布局 | 12 项固定材料/预算/消息块和5项计划/审查/旧新重放/损坏版本验收；相关回归共150项；字节前缀不等于缓存命中 | [业务布局](test_context_layout.py)、[布局对照](../test_context_layout.py) |
| B03.1 缓存与输入观测 | 29 项基础、14 项业务：JSON/SSE、真实工具、缓存与总量分离、前缀、摘要/主子、故障/保存失败、日志校验及旧新重放；相关回归共156项 | [test_cache_observation.py](test_cache_observation.py)、[基础契约](../test_model_observation.py) |
| B02.3 显式评估与报告 | 29 项场景覆盖实际来源/效果/用量、未知与未暴露、人工引用判读、API 失败后证据保留、捕获失败不请求、批次停止/隔离及报告零网络零修改；真实质量待验收 | [test_injection_evaluation.py](test_injection_evaluation.py) |
| B02.2 成对权限流程 | 14 个变体真实经过文件/MCP/记忆/Skill，模式与实际批准保持；正常读/写/命令成功、敏感内容未泄露、用量/来源记录；11 项夹具保存和批准变更拒绝 | [test_injection_boundaries.py](test_injection_boundaries.py)、[夹具边界](../test_injection_fixture.py) |
| B02.1 成对评估样本 | 版本/来源哈希、5 类入口与正常读取/编辑/命令判据、只读 CLI、损坏/冲突/伪造变量拒绝；17 项检查，未运行模型效果评估 | [test_injection_cases.py](test_injection_cases.py) |
| B01.3 循环停止整体验收 | 真实文件版本/批准后冲突、合法复查、并发清理与未交付恢复、双子任务隔离、伪造观察；9 项业务及 2 个真实强杀边界 | [业务验收](test_no_progress_acceptance.py)、[强杀恢复](test_no_progress_recovery.py)、[独立进程](support/no_progress_worker.py) |
| B01.2 聊天重复失败控制 | 按需工具→提醒→停止、修复/换参、预算/取消/保存失败、反思、长命令附件、子任务阈值与离线重放；12 项业务、11 项适配边界 | [生产场景](test_no_progress_loop.py)、[配置与边界](../test_no_progress_integration.py) |
| B01.1 连续失败策略 API | 真实缺失文件、外部修复、重复读取/翻页、非零进程与批准边界；生产聊天接入见 B01.2 | [业务 API](test_no_progress.py)、[策略边界](../test_no_progress.py) |
| T12.3 发现整体验收 | 同任务体积/请求对照，真实 MCP 与 Skill、会话重开、扩展预算、路径/Hook、主子隔离和共享用量；7项业务场景 | [test_tool_discovery_acceptance.py](test_tool_discovery_acceptance.py) |
| T12.2 聊天按需发现 | 默认首请求、实际读取、提前猜名/同批编辑、每轮重置/预览、预算/保存失败/批准、模式过滤与离线重放；12项业务，13项配置/分页/日志边界 | [业务流程](test_tool_discovery_loop.py)、[基础边界](../test_tool_discovery_integration.py) |
| T12.1 工具目录与可见性 | 分组/模式/整批选择、固定 Schema、预览隔离、真实路径与批准/子任务限制；13项 API 业务场景，聊天接入见 T12.2 | [业务验收](test_tool_discovery.py)、[13项基础边界](../test_tool_catalog.py) |
| T11.3 提案整体验收 | 双子任务冲突/非冲突、拒绝、外部改动、失败隔离和跨会话来源；8个真实强杀边界、重启零执行、显式应用/撤销，共14项场景 | [协作场景](test_proposal_acceptance.py)、[强杀恢复](test_proposal_recovery.py)、[独立进程入口](support/proposal_worker.py) |
| T11.2 提案协作与应用 | 实际子线程、批准/检查点/撤销、拒绝/模式/额度、版本/权限、分页、规则/Hook、计划证据/审查限制、保存故障及来源篡改；30项业务场景 | [test_proposal_application.py](test_proposal_application.py) |
| T11.1 修改提案 API | 实际读取/当前权限与版本、分段/BOM/CRLF、伪造/跨轮/截断拒绝、唯一替换、数量、保存失败与重启零写入；27项 API 业务场景，生产接入见 T11.2 | [test_edit_proposals.py](test_edit_proposals.py)、[22项协议边界](../test_proposals.py) |
| T10.3 代码定位整体验收 | 真实项目连续定位、无源码执行、重启/轮中故障、权限/规则/Hook/模式/取消/额度及实际子任务；12项场景 | [test_python_code_acceptance.py](test_python_code_acceptance.py) |
| T10.2 代码查询工具 | 本地导入/词法绑定、遮蔽和动态未知、歧义与版本刷新、完整分页、参数/引用数量限额、真实模型工具流程及日志；32项场景 | [test_python_code_tools.py](test_python_code_tools.py) |
| T10.1 Python 结构索引 | 真实作用域/别名/位置、同时间戳更新与删除、解析和读取故障、权限撤销、真实链接、取消与资源限额、顶层代码不执行；23项场景 | [test_python_code_index.py](test_python_code_index.py) |
| T9.3 重放整体验收 | 完整失败/未知用量、流式、首次差异/缺失优先级、真实编辑、保存故障及五个真实强杀边界；25项新增场景 | [test_turn_replay_acceptance.py](test_turn_replay_acceptance.py)、[test_recording_recovery.py](test_recording_recovery.py)、[强杀入口](support/recording_worker.py) |
| T9.2 离线重放 | 计划/反思/摘要真实运行、共享预算、子任务与审查、记忆规则和 Skill 冻结、首个差异、CLI 关闭及独立进程零外部动作；11项场景 | [test_turn_replay.py](test_turn_replay.py) |
| T9.1 单轮录制 | 原模型/工具/流式/计划与会话增量、凭据排除、失败不重跑、显示异常及保存失败保留原异常；46项新增场景 | [test_turn_recording.py](test_turn_recording.py)、[格式与上限](../test_recordings.py) |
| T8.3 附件整体验收 | 相同日志输入对照、读取超预算、实际发布故障、备份迁移、四个真实强杀边界且不重跑；8项场景 | [test_artifact_acceptance.py](test_artifact_acceptance.py)、[test_artifact_recovery.py](test_artifact_recovery.py)、[进程入口](support/artifact_worker.py) |
| T8.2 工具附件 | 真实长命令与目录、预览/分页、来源/日志故障、主子隔离、关闭/参数及重启；34项新增场景 | [test_tool_artifacts.py](test_tool_artifacts.py)、[test_artifact_subagents.py](test_artifact_subagents.py)、[test_artifact_arguments.py](test_artifact_arguments.py)、[配置与来源](../test_artifact_integration.py) |
| T8.1 附件存储 | 长文本重启分页、完整性/配额、固定版本、会话隔离、真实 IO 故障和并发；当前为显式存储 API | [test_artifact_storage.py](test_artifact_storage.py)、[格式边界](../test_artifacts.py) |
| T7.3 撤销整体验收 | 独立进程真实编辑/撤销、六个强杀边界、恢复发布中断、重复撤销、未知编辑和会话锁；15项场景 | [test_undo_recovery.py](test_undo_recovery.py)、[进程入口](support/undo_worker.py) |
| T7.2 用户撤销 | 实际编辑自动快照、逆向批准、当前版本/权限、拒绝和故障、取消、来源、无模型消费、主/子副本访问保护 | [test_undo.py](test_undo.py)、[test_checkpoint_access.py](test_checkpoint_access.py)、[状态边界](../test_undo_log.py) |
| T7.1 编辑检查点 | 原字节和来源、真实编辑与重启、未知/失败不升级、写入故障与损坏拒绝；自动接入和撤销另见 T7.2 | [test_edit_checkpoints.py](test_edit_checkpoints.py)、[存储边界](../test_checkpoints.py) |
| T6.3 并发组合验收 | 真实 Git/文件无副作用、规则/Hook/模式/额度组合、双子任务独立取消及 CLI/SIGINT 后线程回收；旧取消观察避免 Windows 索引竞争 | [test_parallel_tool_acceptance.py](test_parallel_tool_acceptance.py)、[test_parallel_subagents.py](test_parallel_subagents.py)、[test_cancellation.py](test_cancellation.py)、[test_subagent_recovery.py](test_subagent_recovery.py) |
| T6.2 并发工具循环 | 生产读取并发、实际 Hook、混合动作与审批、所有日志线程身份、结果配对、日志故障及文件/Git 取消 | [test_parallel_tool_loop.py](test_parallel_tool_loop.py)、[test_read_cancellation.py](test_read_cancellation.py)、[注册策略](../test_parallel_registry.py) |
| T6.1 只读并发调度 | 实际文件读取重叠、最多两项、空闲槽补位、有序回调与串行屏障；故障、取消、清理失败及禁止重入；聊天接入另见 T6.2 | [test_parallel_tool_scheduling.py](test_parallel_tool_scheduling.py)、[基础边界](../test_tool_scheduler.py) |
| T5.3 Hook 示例与组合验收 | 真实目录保护/语法检查、审批与模式、规则等待、关闭零进程、外部改动及真实 CLI/SIGINT 取消 | [test_hook_acceptance.py](test_hook_acceptance.py)、[test_cancellation.py](test_cancellation.py) |
| T5.2 工具 Hook 边界 | 主/子继承、来源与阶段、拒绝无动作、审批保持、后置结果不丢失、日志故障和真实进程取消 | [test_tool_hooks.py](test_tool_hooks.py)、[日志校验](../test_hook_log.py) |
| T5.1 本地 Hook 进程 | 真实 JSON 通信、环境限制、关闭零进程、超时/坏响应/非零/大小上限及阻塞输入取消 | [test_hook_process.py](test_hook_process.py)、[配置与协议](../test_hooks.py) |
| T4.3 模式组合验收 | 不可信指导与模型不能切换、MCP 只读、act 拒绝、摘要后限制、模式记录损坏/缺省及真实进程取消 | [test_mode_acceptance.py](test_mode_acceptance.py)、[test_cancellation.py](test_cancellation.py) |
| T4.2 模式切换与继承 | 本地指令、规划后批准执行、任务期间固定、失败解除占用、会话隔离及子线程续聊；记忆对照沿用相同模式 | [test_execution_modes.py](test_execution_modes.py)、[test_memory_acceptance.py](test_memory_acceptance.py) |
| T4.1 模式策略 | 规划时读取可用、编辑/命令无副作用、act 保留审批、未声明扩展拒绝及日志边界 | [test_mode_policy.py](test_mode_policy.py) |
| T3.3 Skill 组合验收 | 跨项目与新会话、真实子任务权限、轮中快照、失效覆盖、流式取消及自动摘要 | [test_skill_acceptance.py](test_skill_acceptance.py) |
| T3.2 Skill 显式启用 | 列表零请求、首请求带正文、每轮独立、预算拦截、用户目录配置和损坏快照 | [test_explicit_skills.py](test_explicit_skills.py) |
| T3.1 Skill 多来源 | 项目优先、覆盖项可见、冻结来源、规范化同名、独立目录授权和合并上限 | [test_skill_sources.py](test_skill_sources.py) |
| T2.3 标准 Skill 整体验收 | 两份实际示例、脚本批准/拒绝、子权限保持、源材料变化后重启 | [test_standard_skill_acceptance.py](test_standard_skill_acceptance.py)、[test_skill_boundaries.py](test_skill_boundaries.py) |
| T2.2 统一 Skill 加载 | 混合格式、材料按需、Unicode ID、版本变化、文本上限、实际请求和重启日志 | [test_standard_skill_loading.py](test_standard_skill_loading.py) |
| T2.1 标准 Skill 元数据 | 标准/Unicode 字段、正文按需、双来源拒绝、恶意 YAML、目录和链接边界 | [test_standard_skill_metadata.py](test_standard_skill_metadata.py) |
| T1.3 规则整体验收 | 预览无副作用、会话/重启隔离、旧快照保留、日志篡改拒绝、新规则超预算和关闭配置 | [test_project_rule_acceptance.py](test_project_rule_acceptance.py) |
| T1.2 项目规则执行 | 主/子规则隔离、新规则先于编辑/命令、整批等待、变更/删除、审批与预算 | [test_project_rule_execution.py](test_project_rule_execution.py) |
| T1.1 项目规则读取 | 根/子作用域、版本与删除、坏编码、大小、链接和范围伪造 | [test_project_rules.py](test_project_rules.py) |
| E7.3 故障与对照 | 真实断开/超时/错误/取消、无重发、进程退出、Schema/凭据边界、成本测量 | [test_mcp_failures.py](test_mcp_failures.py) |
| E7.2 MCP 工具 | 原生/协议查询一致、实际工具记录、嵌套 Schema、冲突清理 | [test_mcp_tools.py](test_mcp_tools.py) |
| E7.1 MCP 连接 | 官方 SDK 真实进程握手、复用、关闭、未启动和配置校验 | [test_mcp_connection.py](test_mcp_connection.py) |
| E6.3 权限与成本 | 恶意指导不跳过审批、子权限保持、超预算不请求、真实示例大小对照 | [test_skill_boundaries.py](test_skill_boundaries.py) |
| E6.2 渐进加载 | 初始只含目录、匹配与无关任务、正文版本和重启记录 | [test_skill_loading.py](test_skill_loading.py) |
| E6.1 Skill 目录 | 元数据独立读取与空白规范化、包内范围、更新失效、大小与链接边界 | [test_skill_catalog.py](test_skill_catalog.py) |
| E5.3 协作验收 | 真实缺陷修改与复审、已正确文件的无收益对照 | [test_review_acceptance.py](test_review_acceptance.py) |
| E5.2 串行审查 | 独立证据、伪造/过期拒绝、外部删除可收集、串行限制、次数与共享消费 | [test_independent_review.py](test_independent_review.py) |
| E5.1 审查记录 | 来源、版本与真实行号、并行拒绝、重启不重跑 | [test_review_records.py](test_review_records.py) |
| E4.6 对照验收 | 真实失败→反思→编辑→检查、新任务、差值反例、离线评估入口 | [test_reflection_acceptance.py](test_reflection_acceptance.py)、[手动评估](../../docs/reflection-evaluation.md) |
| E4.5 经验复用 | 新会话、范围与相关性、版本/过期/删除、预算和子继承 | [test_experience_reuse.py](test_experience_reuse.py) |
| E4.4 经验候选 | 查看/改写/接受/丢弃、保存失败恢复、重启和删除不复活 | [test_experience_candidates.py](test_experience_candidates.py) |
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

T2.3 回归核对：未交付结果场景通过实际工作线程的 Future 同步，不再从外部高频打开索引文件，避免观察者制造 Windows 替换冲突；模型、文件与调度均实际运行，未提前调用 wait_agents。


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

B32.2：test_prefix_regression.py 验收决策前文件恢复、替代动作集合与前缀变化拒绝；模型为内存替身，文件真实读写，2 passed（0.32秒）。

B32.3：test_prefix_modes.py 验收旧响应/新决策区别、响应串用/变化以及外部副作用拒绝；B32三个文件共6 passed（1.18秒），未调用付费模型。

B33.1：test_rubric.py 通过真实修复验证器和缺失/矛盾证据验收四维评分；2 passed（25.32秒），不调用模型。

B33.2：test_rubric_calibration.py覆盖六样本金标、误报/漏报/缺失及顺序偏差；2 passed（0.09秒），不调用模型。

B33.3：test_rubric_contamination.py验证评分载荷、来源伪造、缺来源及交换后一致污染；B33章末三个文件6 passed（0.76秒）。

B34.1：test_phase_timing.py覆盖真实应用阶段采集、首输出、实际文件操作失败和审批嵌套区间；2 passed（2.10秒）。

B34.2：test_phase_report.py验收CLI、分布、缓存费用和未知；加真实入口定向共2 passed（2.09秒）。

B34.3：test_phase_comparison.py覆盖两组重复、实际缓存日志、缺失/重叠和截断；B34章末及B31直接受影响入口/报告共8 passed（12.59秒）。

B35.1：test_failure_tasks.py覆盖三条构造失败来源、六个变体实际文件验证及额外产物拒绝；1 passed（31.77秒）。

B35.2：test_failure_suite.py验证整族隔离、新目录重置及B31实际应用；含默认入口兼容共2 passed（3.77秒）。

B35.3：test_failure_task_boundaries.py覆盖错误判据、无效/重复变体、变化来源和宿主答案越界读取；本章及B31入口共6 passed（9.49秒）。

B36.1：test_experience_evidence.py覆盖真实来源关联、正反证据、旧版本保持及来源/环境变化；1 passed（0.55秒）。

B36.2：test_experience_validation.py验证新任务实际文件、错误答案、执行故障和提炼来源拒绝；1 passed（0.46秒）。

B36.3：test_experience_regression.py覆盖实际文件对照、反例推翻、环境变化及真实E4接受；章末4 passed（2.47秒）。

B37.1：test_skill_proposal.py验证真实SkillCatalog兼容、原目标保留及新建/更新来源信息；1 passed（0.50秒）。

B37.2：test_skill_release.py实际加载Skill/编辑文件后双集比较，覆盖显式拒绝/接受/回滚；1 passed（7.20秒），HTTP模拟不证明模型效果。

B37.3：扩展test_skill_release.py复用双集产物覆盖过期、批准后冲突、原子写入失败与保护目标；章末及默认B31入口4 passed（12.30秒）。

B38.1：test_trusted_verifier.py验收真实文件、模型虚假成功、标准快照及未知状态；2 passed（0.67秒）。

B38.2：test_completion_verification.py实际工具批准修正及同轮验收记录、两次修正上限；2 passed（2.76秒）。

B38.3：test_completion_verification_faults.py验证正确初稿、取消、两类保存失败及请求额度；本章功能与依赖32 passed（5.69秒）。

B39.1：test_exploration_group.py 覆盖错误先到、真实证据胜出、重复/重启读取及保存失败不发布 winner；2 passed（0.32秒）。

B39.2：test_exploration_dispatch.py 经模拟HTTP、真实子线程和取消令牌核验 winner 与 interrupted 状态；连同组基础3 passed（25.84秒）。

B39.3：扩展test_exploration_group.py与test_exploration_dispatch.py覆盖并发唯一winner、损坏快照、错误先到、取消/完成竞争和重启预算；加既有委派章末13 passed（14.10秒）。

B40.1：test_private_workspaces.py 使用两个实际Git副本验证主项目不变、规则/记忆身份、权限收窄、脏源拒绝及清理路径篡改；2 passed（8.80秒）。

B40.2：test_private_merge.py 验证子HTTP→副本真实双文件编辑→主项目保持不变→逐文件批准合并→独立验收→关闭清理；1 passed（6.94秒），原委派权限场景1 passed（2.53秒）。

B40.3：test_private_task_boundaries.py 实际双子模型与文件验证冲突/非冲突、越界/取消；test_private_recovery.py 验证独立进程强杀后清理与原项目嵌套规则。章末含既有委派和架构38 passed（47.65秒）。

B41.1：test_role_profiles.py 检查宿主模型/请求上限、只读角色实际拒绝编辑、执行者仍需批准且遵守plan模式；2 passed（1.00秒）。

B41.2：test_role_handoff.py 覆盖真实角色工具越权拒绝、执行批准、无关历史不复制、模式变化拒绝、API失败/重复执行拒绝及遗留状态恢复；3 passed（4.98秒）。

B41.3：test_role_comparison.py 经实际Skill加载、工具编辑和角色移交对照两任务，并验证调查用完请求后不发执行请求；B41章末含架构29 passed（12.19秒）。

B42.1：test_delegation_policy.py 参数化8项覆盖简单/独立任务、在途预留、主任务余量、未知和待回收优先；8 passed（0.15秒）。

B42.2：test_budgeted_dispatch.py 在真实工具阶段注入共享预留/未知成员，验证旧建议失效及过低估算不能发出实际子HTTP请求；3 passed（4.35秒）。

B42.3：test_delegation_comparison.py 对照简单/独立任务，检验真实预算工具来源、主子用量合计、未知子消费/失败收尾和分母保留。章末39项通过，3项旧接法改用正式delegation_dispatch后通过（4.48秒），合计42场景；不重复跑其余已通过项。
