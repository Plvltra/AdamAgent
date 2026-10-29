# Adam 系统架构

最近核对：2026-10-29

本次核对范围：按 E 编号整理 main 提交历史，架构无变化；业务代码、测试、配置与依赖内容保持一致。ReviewService 分开处理历史读取凭据与当前目标有效性，文件删除后仍能收集为 stale；SkillCatalog 在目录与请求校验前统一去除名称、用途两端空白。保留 ToolExecutor 协作限制回调的契约说明。

核对结论：新增 ExperienceService 连接会话候选与项目记忆；CLI 经 facade 调用，不接触两种存储。MemoryRecord 保存可选经验来源，更新/删除保留来源，MemoryStore 在锁内去重。两个文件间失败由来源对账，不自动重放模型。聊天物理请求移入 RequestRunner，ReflectionService 共用登记、预算、取消与结算；SessionLog 增加专门来源校验和 ReflectionLog。主循环整批工具保存后调用 ReflectionService；bootstrap 仅给主应用注册 report_feedback。ContextBuilder 接收本轮反思参考资料并计入预算，SessionLog 校验其真实来源，CLI 预览展示对应估算。验证主计划工具与委派共存，子应用没有计划工具；完成依据来自实际工具、编辑、命令及子结果。CLI 通过应用查询 PlanReport；工具结果落盘后的回调负责展示，展示异常不进入工具错误转换。Conversation 通过 TaskPlanner 判断待办和一次提醒，固定提醒仅作用于当前进行中的轮次。新增 contracts/planning.py 与 session/plan_log.py，计划使用现有日志和恢复，不新增文件存储或独立恢复器。主应用已接入 PlanningService 和 update_plan；追加及回放验证真实工具证据，子应用不注册计划工具。MemoryService 经回调为 Conversation 提供每轮快照，ContextBuilder 计入预算并构造资料消息；请求日志保存版本与内容。管理命令仍经应用接口；SubagentManager 按派发捕获主轮次快照，bootstrap 注入子 Conversation，子工具保护共享记忆目录。MemoryService 仅降级 MemoryStoreError，其他存储与执行错误保持原边界。委派模块结构保持；恢复索引时以完整子 JSONL 为准，已提交回答恢复成功，未完成者标中断且不重发。主任务退出先清理所有子任务；清理失败保留原异常。结果按实际 JSON 长度限制，预览其他会话不引用旧子任务。

本文描述已实现代码。版本任务见 [练手 TODO](agent-practice-todo.md)，日志格式见 [会话执行轨迹](session-tracing.md)。本次问题及修正见 [依赖边界重构](dependency-refactor.md)。

## E2 文档检索

```mermaid
flowchart LR
    TEXT["documents/text.py<br/>分块与分词"] --> VALUES["documents/models.py<br/>Chunk / SearchHit"]
    RANK["documents/retrieval.py<br/>关键词排序"] --> TEXT
    RANK --> VALUES
    BOOT["bootstrap.py"] --> TOOLS["tool_calls/document_tools.py<br/>注册、适配和结果限长"]
    TOOLS --> REGISTRY["tool_calls/registry.py<br/>参数校验"]
    TOOLS --> SEARCH["documents/search.py<br/>DocumentSearch"]
    CLI["document_search.py<br/>独立查询/模型准备/评估"] --> SEARCH
    SEARCH --> RANK
    SEARCH --> VECTOR["documents/vectors.py<br/>VectorSearch"]
    VECTOR --> EMBED["documents/embedding.py<br/>FastEmbedBackend / EmbeddingBackend"]
    VECTOR --> SNAP["documents/snapshots.py<br/>有界校验与原子保存"]
    CANCEL["子任务 CancellationToken.check"] -.-> SOURCE
    CANCEL -.-> INDEX
    CANCEL -.-> VECTOR
    INDEX --> SNAP
    SEARCH --> INDEX
    INDEX["documents/index.py<br/>DocumentIndex"] --> SOURCE["documents/source.py<br/>有界扫描"]
    SOURCE --> AUTH["注入的路径授权函数"]
    INDEX --> TEXT
    INDEX --> CACHE["JSON 快照 + 文件锁 + 原子替换"]
    SOURCE --> CONFIG["config/settings.py<br/>DocumentConfig"]
```

DocumentIndex 持锁刷新配置范围内的源文件；源文件经授权和限额检查后计算内容版本。缓存损坏或读写失败明确报错，不返回旧资料。仅变更文件重新分块。搜索通过普通工具结果进入上下文与会话日志；不修改 Conversation。子任务逐个授权源文件，主子文件工具均保护索引目录。embedding 延迟加载，仅显式 prepare-model 可下载；查询失败不自动切策略。独立 document_search 入口不读取聊天密钥，evaluation 按固定标注统计候选证据。

## E7.1～E7.2 MCP 服务接入

~~~mermaid
flowchart LR
    BOOT["bootstrap.py"] --> RES["ExitStack"]
    APP["application/facade.py"] -->|close| RES
    BOOT --> TOOL["tool_calls/mcp_tools.py"]
    TOOL --> REG["tool_calls/registry.py<br/>可选参数校验器"]
    REG --> SCHEMA["mcp/schema.py"]
    TOOL --> CLIENT["mcp/client.py"]
    CONF["config/mcp.py"] --> CLIENT
    CLIENT --> PORTAL["AnyIO BlockingPortal"]
    PORTAL --> SDK["官方 mcp.Client"]
    SDK -->|stdio| SERVER["本地服务进程"]
~~~

默认关闭，程序和工具允许列表由配置指定；模型不接触进程参数。失败/取消后不复用连接，重启应用才能重新连接。

## E6.1～E6.2 Skill 目录与按需加载

~~~mermaid
flowchart LR
    BOOT["bootstrap.py"] --> TOOL["tool_calls/skill_tools.py"]
    TOOL --> CAT["skills/catalog.py"]
    BOOT --> CONV["application/conversation.py"]
    CONV --> BUILDER["context/builder.py"]
    BUILDER --> CONTRACT["contracts/skills.py"]
    LOG["session/log.py"] --> CONTRACT
    CAT["skills/catalog.py"] --> META["skills/id/skill.toml<br/>启动只读元数据"]
    CAT -->|按需 load| BODY["SKILL.md / 已登记材料"]
    CAT -->|注入 resolve| POLICY["路径权限"]
    CAT --> ERR["contracts/errors.py"]
~~~

主/子应用都可只读加载，tools 不提供新权限。目录单独计入 skill_tokens，正文作为工具消息沿用 turn/history 预算；日志回放不读当前文件。返回完整内容与 SHA-256，超长报错，包外材料拒绝。

## E5.1～E5.2 独立审查

~~~mermaid
flowchart LR
    CONV["application/conversation.py"] --> SERVICE["application/review.py"]
    TOOL["tool_calls/review_tools.py"] -->|Reviewer 接口| SERVICE
    EXEC["tool_calls/executor.py"] -->|check_call| SERVICE
    SERVICE --> MANAGER["application/subagents/manager.py"]
    SERVICE --> STORE
    SERVICE --> POLICY["tool_calls/policy.py + text_file.py"]
    MANAGER["application/subagents/manager.py"] --> CHILD["子 SessionStore<br/>已结束轮次的真实工具记录"]
    MANAGER --> INDEX["session/delegation.py<br/>helper / reviewer"]
    STORE["session/storage.py"] --> LOG["session/log.py"]
    LOG --> REVIEW["session/review_log.py"]
    REVIEW --> CONTRACT["contracts/review.py<br/>来源、结论及读取依据"]
~~~

主执行和只读审查复用 V6；审查状态与子回复是否成功保存分开。恢复只关闭 pending，不重发请求。

## E1.1～E1.4 记忆存储、管理、上下文与继承

```mermaid
flowchart LR
    CLI["cli/memory_commands.py"] --> APP["application/facade.py<br/>AgentApplication"]
    APP --> SERVICE["application/memories.py<br/>MemoryService"]
    BOOT["bootstrap.py"] --> SERVICE
    SERVICE --> PROJECT
    SERVICE --> STORE
    CONV["application/conversation.py"] -->|每轮选择一次| SERVICE
    SERVICE --> RANK["memory/retrieval.py<br/>rank_records"]
    SERVICE -->|输入估算| BUILDER["context/builder.py"]
    CONV -->|固定快照| BUILDER
    CONV -->|set_memory| MANAGER["application/subagents/manager.py"]
    MANAGER -->|派发时捕获快照| CHILD["子 Conversation"]
    CHILD --> BUILDER
    BOOT -->|共享记忆目录保护| POLICY["tool_calls/policy.py"]
    SERVICE -->|读取失败转 unavailable| ERR
    CONV --> LOG["session/log.py<br/>验证请求中的记忆快照"]
    LOG --> RECORD
    STORE["memory/store.py<br/>MemoryStore"] --> RECORD["contracts/memory.py<br/>MemoryRecord / MemorySource"]
    STORE --> ERR["contracts/errors.py<br/>MemoryStoreError / MemoryInputError"]
    PROJECT["memory/project.py<br/>project_identity"] --> ERR
    STORE --> FILE["memories.json<br/>跨进程锁 + 原子版本快照"]
```

命令解析与展示归 CLI，范围、来源与管理归 MemoryService；CLI 不直接导入 memory 包。记忆存储不依赖会话存储或模型；来源会话 ID 只作为可选值保留。MemorySelection 与记录校验位于 contracts/memory.py，存储和请求日志复用契约，避免两个存储模块互相导入。包依赖测试已覆盖 memory 及应用对它的单向依赖。

## E4.1～E4.3 反馈与反思请求

~~~mermaid
flowchart LR
    CONV["application/conversation.py"] --> RUNNER["application/request_runner.py"]
    CONV -->|整批工具后| SERVICE["application/reflection.py"]
    BOOT["bootstrap.py"] --> SERVICE
    TOOL["tool_calls/feedback_tools.py"] -->|FeedbackReporter| SERVICE
    CONV -->|本轮参考资料| BUILDER["context/builder.py"]
    SERVICE --> RUNNER
    RUNNER --> PORT["contracts/ports.py<br/>ModelClient"]
    RUNNER --> STORE["session/storage.py"]
    SERVICE --> STORE
    CLI["cli/memory_commands.py"] --> APP["application/facade.py"]
    APP --> EXP["application/experiences.py"]
    EXP --> STORE
    EXP --> MEMORY["memory/store.py<br/>来源幂等写入"]
    STORE --> LOG["session/log.py"]
    LOG --> REFLECT["session/reflection_log.py"]
    LOG --> FEEDBACK["session/feedback_log.py"]
    FEEDBACK --> CONTRACT["contracts/reflection.py<br/>Feedback"]
    STORE -->|有界事实投影| FEEDBACK
    STORE --> JSONL["会话 JSONL<br/>feedback_recorded"]
~~~

反馈诊断与成功历史分别保存，查询不调用模型；存储实现不依赖应用服务。阶段状态见 [E4](reflection-and-experience.md)。

## E3.1～E3.3 计划记录、工具与交互

```mermaid
flowchart LR
    BOOT["bootstrap.py"] --> SERVICE["application/planning.py"]
    CONV["application/conversation.py"] -->|开始轮次、收尾检查与提醒| SERVICE
    CLI["cli/main.py"] -->|/plan| APP["application/facade.py"]
    APP -->|load_plan| STORE
    CLI --> VIEW["cli/terminal.py<br/>show_plan"]
    EXEC["tool_calls/executor.py"] -->|工具结果落盘后 publish| SERVICE
    SERVICE -->|经 bootstrap 注入展示回调| VIEW
    TOOL["tool_calls/planning_tools.py"] --> CONTRACT["contracts/planning.py<br/>TaskPlanner"]
    SERVICE -.-> CONTRACT
    TOOL -->|注入接口| SERVICE
    SERVICE --> STORE["session/storage.py"]
    STORE --> LOG["session/log.py"]
    LOG --> EVIDENCE["session/plan_evidence.py<br/>实际工具及命令/编辑结果"]
    LOG --> PLAN["session/plan_log.py<br/>PlanLog"]
    PLAN --> DATA["contracts/planning.py<br/>PlanSnapshot / PlanReport"]
    STORE --> DATA
    STORE --> JSONL["会话 JSONL<br/>plan_updated"]
```

计划事件与请求、工具关联，版本回放不访问磁盘。读取报告结合所属轮次的终态；中断恢复沿用 session_recovered，不重跑计划。模型工具和界面均已接入；未闭合时主循环暂存流式正文并最多提醒一次，额度不足或再次遗漏时保留回答并明确提示未完成。完整状态见 [E3](task-planning.md)。

## 包目录与依赖边界

同一能力的文件放在同一个包内，包内仍按职责分文件。根目录的 `__main__.py`、`bootstrap.py`、`diagnostics.py` 分别负责聊天启动、资源组装和原诊断入口；`document_search.py` 负责独立的文档查询、模型准备与评估，不组装聊天应用或读取其密钥。

| 包 | 归入的文件 | 允许的其他项目依赖 |
| --- | --- | --- |
| cli | main.py、terminal.py、reply_output.py、memory_commands.py | application、context、config、contracts；仅 main 调用 bootstrap |
| application | facade.py、conversation.py、request_policy.py、memories.py、experiences.py、planning.py、reflection.py、request_runner.py、subagents/manager.py、budget.py、account.py | context、session、memory、tool_calls、config、contracts |
| config | loader.py、settings.py | contracts |
| context | builder.py、pruning.py、compaction.py | session、config、contracts |
| llm | client.py、streaming.py、http_transport.py | config、contracts |
| session | storage.py、log.py、compaction_log.py、edit_log.py、plan_log.py、plan_evidence.py、feedback_log.py、reflection_log.py、review_log.py、outcome.py、execution_report.py、diagnostics.py、delegation.py | contracts |
| documents | 分块、来源、文本/向量检索、快照、embedding 与 evaluation | config、contracts |
| memory | store.py、project.py、retrieval.py | contracts |
| tool_calls | 工厂、注册表、执行器、权限、document_tools.py、planning_tools.py 和具体工具 | documents、session、config、contracts |
| contracts | models.py、history.py、errors.py、ports.py、protocol.py、delegation.py、cancellation.py、memory.py、planning.py、reflection.py、review.py | 无 |

包内使用绝对路径显式导入所属实现；类型和错误直接从定义位置导入，避免经过其他业务模块转手。新包的 `__init__.py` 仅说明职责，不批量加载实现。contracts 只承载现有跨模块契约，不作为任意辅助函数目录。

bootstrap 可创建各层具体组件，业务包不反向调用它。cli/main 不直接依赖 Conversation、存储、HTTP 或具体工具。上下文构造是纯逻辑；同包的摘要服务经模型接口和会话存储完成持久化操作。会话包不依赖上下文算法，摘要事件的校验和恢复仍归 session。

ExecutionReport、ActionFact 放在 contracts/models.py，提取事实的 build_execution_report 留在 session/execution_report.py；展示层只依赖报告值，不连带导入日志解释器。tests/test_architecture.py 检查整包源码，新增文件同样受约束。业务测试继续按功能放在 tests/features。迁移前后收集项比对：原 408 项无遗漏，增加 8 项包依赖约束，共 416 项；本次没有改变业务场景断言。

## V6 委派、额度与资源边界

```mermaid
flowchart LR
    BUDGET["application/subagents/budget.py<br/>SharedTokenBudget"] --> ERRORS["contracts/errors.py"]
    CANCEL["contracts/cancellation.py<br/>CancellationToken"] --> ERRORS
    LOADER["config/loader.py"] --> SUBCONFIG["config/settings.py<br/>SubagentConfig"]
    BOOT["bootstrap.py"] --> MANAGER["application/subagents/manager.py<br/>派发、收集、停止、恢复"]
    BOOT --> CHILD["独立子 Conversation / SessionStore / HTTP"]
    MANAGER --> INDEX["session/delegation.py<br/>原子索引"]
    MANAGER --> BUDGET
    MANAGER --> ACCOUNT["application/subagents/account.py<br/>成员日志结算"]
    ACCOUNT --> BUDGET
    ACCOUNT --> STORE["session/storage.py<br/>独立 JSONL"]
    ADAPTER["tool_calls/delegation_tools.py"] --> PORT["contracts/delegation.py"]
    MANAGER -.-> PORT
    CHILD --> ACCOUNT
    CHILD --> CANCEL
```

共享额度按成员保存持久化累计快照；在途预留只影响新请求准入，不能被当作用量未知。
这些组件已接入下面的聊天调用链。业务计划与使用方法见 [V6](subagents.md)。
主线程持有主会话锁；每次子运行在所属线程新建并关闭自己的存储与传输。
SubagentManager 保留可续聊身份和独立历史，线程池只负责当前运行，容量不足不排队。
SessionAccount 在请求登记前预留、结算日志后更新；普通请求与摘要共用同一整组账本。
root 的 /usage 显示聚合值；未知消费禁止新请求，但不主动取消其他在途请求。

委派索引关联父轮次/请求/工具及子轮次，保存位置独立于主会话日期目录；
子任务正文只通过 tool 消息返回主模型。最终回答前未收集的结果由协调接口检查，
主循环在既有请求限额内提醒模型读取，不虚构模型工具调用。
子工具目录没有委派能力，命令/编辑适配器直接返回 approval_required，不接终端审批。
主轮次失败或取消后先停止并等待所有子线程，再释放主会话锁。索引恢复从子日志的 child_turn_id 查找已提交历史或失败终态；没有完整结束记录才标为中断。索引缺失但子日志存在时拒绝静默初始化，避免遗漏消费。

取消令牌关闭子线程当前 HTTP 等待；清理连接时不再次检查已置位的令牌。文件/Git 等同步动作沿阶段边界协作取消。真实单独/整组取消、其他终端隔离及两个强杀边界由 tests/features/test_subagent_recovery.py 验证；整体图中没有新增依赖边，原有约束保持。

## 启动与资源所有权

实线表示主要调用、委托或读写关系，虚线表示协议校验或展示数据依赖；这是职责关系图，不是严格执行时序或完整 import 清单。bootstrap 是依赖组装与资源所有权入口；CLI 只解释输入和展示结果。

```mermaid
flowchart TD
    ENTRY["__main__.py<br/>程序入口"] --> CLI["cli/main.py<br/>用户交互、指令处理"]
    CLI --> BOOT["bootstrap.py<br/>组装与资源关闭"]
    BOOT --> CONFIG["config/loader.py<br/>加载与校验配置"]
    CONFIG --> SETTINGS["config/settings.py<br/>不可变配置"]
    BOOT --> APP["application.facade.AgentApplication"]
    CLI --> APP
    BOOT --> LLM
    BOOT --> STORE
    BOOT --> FACTORY["tool_calls.factory<br/>组装内置工具"]
    BOOT --> DELEGATE["application/subagents/manager.py"]
    CONV -->|委派生命周期与结果收集检查| DELEGATE
    DELEGATE --> DINDEX["session/delegation.py"]
    BOOT --> DTOOLS["tool_calls/delegation_tools.py<br/>仅主 Agent 注册"]
    DTOOLS -.-> DPORT["contracts/delegation.py"]
    DELEGATE -.-> DPORT
    CLI --> TERMINAL["cli/terminal.py<br/>角色、失败、上下文、计划与执行事实展示"]
    APP --> CONV["application/conversation.py<br/>轮次收尾、模型—工具循环"]
    APP -->|"会话列表、历史、计划与用量查询"| STORE["session/storage.py<br/>会话持久化与读取"]

    CONV --> COMPACT["context/compaction.py<br/>规划与执行摘要"]
    COMPACT --> STORE
    COMPACT --> PORT
    COMPACT --> SETTINGS
    COMPACT --> HISTORY
    COMPACT -->|"素材估算"| CONTEXT
    COMPACT -->|"自动摘要素材缩减"| PRUNE
    LOG --> EDITLOG["session/edit_log.py<br/>编辑计划、写入状态与恢复投影"]
    LOG --> CLOG["session/compaction_log.py<br/>摘要提交、消费与恢复投影"]
    CLOG --> HISTORY
    CLOG --> OUTCOME
    CONV --> CONTEXT["context/builder.py<br/>构造上下文、组成估算、输入预算"]
    CONTEXT --> HISTORY["contracts/history.py<br/>完整成功轮次数据"]
    STORE --> HISTORY
    LOG --> HISTORY
    CONTEXT --> PRUNE["context/pruning.py<br/>旧工具结果头尾节选"]
    PRUNE --> SETTINGS
    PRUNE --> HISTORY
    CONTEXT --> SETTINGS
    CONTEXT --> ERROR
    TERMINAL -.->|"读取 RequestContext 展示数据"| CONTEXT
    CONV --> RETRY["application/request_policy.py<br/>物理请求额度与有限连接重试"]
    RETRY --> ERROR
    CONV --> RUNNER["application/request_runner.py<br/>请求登记、结算、取消"]
    RUNNER --> PORT
    BOOT --> LLM["llm/client.py<br/>访问模型服务"]
    APP -->|"查询模型目录"| PORT["contracts.ports.ModelClient"]
    LLM -.->|"实现"| PORT
    BOOT --> HTTP["llm.http_transport.InterruptibleTransport<br/>异步 IO 与同步接口桥接"]
    LLM --> HTTP
    HTTP --> API["远端 LLM"]
    LLM --> STREAM["llm/streaming.py<br/>SSE 与完整响应组装"]
    STREAM -.-> PROTOCOL
    LLM -.->|"ModelOutput 回调"| OUTPUT["cli/reply_output.py<br/>增量展示与完整性提示"]
    CLI --> OUTPUT
    CONV -->|"工具说明与执行"| EXEC["tool_calls.ToolExecutor"]
    CONV --> STORE
    EXEC -->|"保存执行记录"| STORE
    STORE --> FILE["会话 JSONL 文件与恢复备份"]
    DIAG["session/diagnostics.py<br/>独立只读报告"] -->|读取快照| FILE
    DIAG --> LOG
    STORE --> LOG["session/log.py<br/>校验关联与状态、整理历史与用量"]
    STORE --> REPORT["session/execution_report.py<br/>从日志提取本轮执行事实"]
    REPORT --> LOG
    REPORT --> VALUES["contracts/models.py<br/>ExecutionReport 与共享值"]
    CONV -.->|"返回执行事实"| VALUES
    APP -.->|"对界面提供执行事实"| VALUES
    TERMINAL -.->|"展示 ExecutionReport"| VALUES
    CONV --> OUTCOME["session/outcome.py<br/>结束原因、耗时与受控错误"]
    STORE --> OUTCOME
    LOG -.->|"校验终态字段"| OUTCOME
    OUTCOME --> ERROR["contracts/errors.py<br/>公共错误契约与原因码"]

    LLM -.->|"校验模型消息"| PROTOCOL["contracts/protocol.py<br/>消息与完整轮次的共同规则"]
    STORE -.->|"校验待保存轮次"| PROTOCOL
    LOG -.->|"校验日志中的完整轮次"| PROTOCOL
```

共享的 `contracts/models.py` 定义 Session、Message、Completion、CommandRequest、SessionUsage、ContinuationStatus 和记录回调类型。`config/settings.py` 只定义配置值；`config/loader.py` 负责读取和校验。这些数据定义不引用 HTTP、文件存储实现或工具执行模块。

## 工具创建与执行

```mermaid
flowchart TD
    E["executor.py<br/>轮次关联、编辑额度与留痕"] --> R["registry.py<br/>工具说明、参数校验与分派"]
    FACTORY["factory.py<br/>创建工具并注册处理函数"] --> R
    FACTORY --> CATALOG["catalog.py<br/>工具说明"]
    FACTORY --> F
    FACTORY --> G
    FACTORY --> C
    FACTORY --> EDIT
    R -->|"注册的处理函数"| F["file_tools.py<br/>目录、文件、搜索与读取"]
    F --> SNAP["text_file.py<br/>有界文本快照与版本"]
    PLAN["edit_plan.py<br/>纯替换计划与 diff"] --> SNAP
    PLAN -.-> P
    R --> EDIT
    EDIT["edit_tools.py<br/>确认与可靠文件替换"] --> PLAN
    EDIT --> SNAP
    EDIT -.-> P
    EDIT -->|"确认回调"| UI
    R --> G["git_tools.py<br/>状态、差异与提交查询"]
    R --> C["command_tools.py<br/>执行外部程序并收集结果"]
    C -->|"请求用户确认"| UI["cli/main.py<br/>展示命令或 diff，取得 y/N"]

    FACTORY --> P["policy.py<br/>路径规则、读写权限"]
    F -.-> P
    G -.-> P
```

`PathPolicy` 分别检查读取和写入范围；读取授权不赋予写入权限，专用编辑禁止 .git、凭据及链接路径。通用命令单独展示具体命令和工作目录，请用户逐次确认；批准后的程序按当前系统账户权限运行，额外读取目录配置不是命令进程的沙箱边界。

`CommandTools` 只需要工作目录、资源限制和确认回调，不接收与其权限模型不相符的 PathPolicy。批准的通用命令按当前系统账户权限运行；额外读取目录只限制只读工具，不是命令进程的操作系统沙箱。

`ToolExecutor` 负责调用所属轮次、请求和执行轨迹；`ToolRegistry` 负责工具目录、参数校验与分派；具体工具负责动作本身。命令和编辑通过记录回调把副作用边界交给协调层保存，通过确认回调取得用户决定。拒绝结果保存后停止同批剩余调用及本轮请求；失败输入不进入有效历史。

ToolExecutor 在开始记录写入后调用工具，在结果落盘后回传模型。命令/编辑沿同一回调上报事件，执行器按 command_* / edit_* 分别记录；注册表不按工具名称分支。日志保存失败仍停止本轮。执行层通知工具名称，`工具>` 等展示文字由 CLI 生成。

## 模块职责

| 模块 | 主要接口 | 职责 |
| --- | --- | --- |
| [__main__.py](../adam/__main__.py) | 程序入口 | 将 `python -m adam` 交给 CLI 启动 |
| [cli/main.py](../adam/cli/main.py) | `main`、`chat`、`handle_command` | 调用组装入口，处理输入和指令，展示增量/结果/错误，取得命令和 diff 确认 |
| [cli/terminal.py](../adam/cli/terminal.py) | `role_prefix`、`show_failure`、`show_context`、`show_execution_report` | 展示角色、失败、上下文和执行事实；不读取会话或请求模型 |
| [session/execution_report.py](../adam/session/execution_report.py) | `build_execution_report` | 从已校验日志提取执行顺序、状态、版本和退出码；不解释模型的测试结论 |
| [contracts/errors.py](../adam/contracts/errors.py) | `AgentError`、`FailureCode`、`BudgetError`、`TurnSaveError` | 定义可展示说明、可选未保存回答及稳定失败类别，保持界面错误接口稳定 |
| [session/diagnostics.py](../adam/session/diagnostics.py) | `read_report`、`main` | 只读单个日志快照，按轮次输出请求和工具详情；不请求模型、不修复文件 |
| [session/outcome.py](../adam/session/outcome.py) | `ExecutionOutcome`、`outcome_details` | 表达请求或轮次的终态、原因和已测耗时；写入和回放共用字段校验，不负责计时或 IO |
| [bootstrap.py](../adam/bootstrap.py) | `open_application`、`build_application` | 组装组件并关闭自己创建的资源 |
| [application/facade.py](../adam/application/facade.py) | `AgentApplication` | 面向界面的会话、模型、上下文、继续方式与执行事实查询 |
| [config/settings.py](../adam/config/settings.py) | 配置值 | 不可变运行配置 |
| [contracts/models.py](../adam/contracts/models.py) | 共享数据、EditPlan、ActionFact 与 ExecutionReport | 不依赖具体实现的跨层数据 |
| [application/subagents/manager.py](../adam/application/subagents/manager.py) | SubagentManager | 委派准入、追问、结果交付、取消与重启恢复 |
| [application/subagents/account.py](../adam/application/subagents/account.py) | SessionAccount | 线程所属日志与整组额度之间的结算边界 |
| [session/delegation.py](../adam/session/delegation.py) | DelegationStore | 原子持久化成员与历次派发索引 |
| [contracts/delegation.py](../adam/contracts/delegation.py) | RequestAccount、Delegation、DelegationTools | 共享额度和委派流程接口，避免工具反向依赖应用 |
| [tool_calls/delegation_tools.py](../adam/tool_calls/delegation_tools.py) | SPECS、handlers | 委派工具说明与参数适配 |
| [contracts/ports.py](../adam/contracts/ports.py) | `ModelClient` | 模型目录与一次请求能力 |
| [tool_calls/factory.py](../adam/tool_calls/factory.py) | `create_tool_registry` | 组装内置工具和编码器 |
| [tool_calls/catalog.py](../adam/tool_calls/catalog.py) | `SPECS` | 模型可见的工具说明 |
| [config/loader.py](../adam/config/loader.py) | `load_config`、`load_llm_config` | 加载并校验运行配置，区分模型连接、循环/消费限额及单次输入预算 |
| [application/conversation.py](../adam/application/conversation.py) | `Conversation`、`TurnProgress` | 管理会话与轮次；依据输入压力协调自动摘要，统一限制摘要和聊天请求，组织工具与提交 |
| [context/builder.py](../adam/context/builder.py) | `ContextBuilder`、`RequestContext`、`ContextEstimate` | 按完整轮次选择连续历史后缀，估算输入、检查上限并提供来源记录；不改写原始消息 |
| [context/pruning.py](../adam/context/pruning.py) | `prune_history` | 缩减旧工具结果副本，保护近期轮次，保留错误状态和来源 |
| [contracts/history.py](../adam/contracts/history.py) | `HistoryTurn`、`Summary` | 成功轮次及有效摘要的共享数据 |
| [context/compaction.py](../adam/context/compaction.py) | `CompactionService`、`CompactionPlan` | 规划有界素材及压缩目标、复用旧工具缩减；执行无工具摘要、独立结算并提交，不修改原历史 |
| [session/compaction_log.py](../adam/session/compaction_log.py) | `CompactionLog` | 校验摘要来源与状态，仅完整提交激活摘要；恢复不猜未知用量 |
| [llm/http_transport.py](../adam/llm/http_transport.py) | `InterruptibleTransport` | 为每个所属线程的同步接口提供可取消的异步 IO；连接池不跨线程共享 |
| [application/request_policy.py](../adam/application/request_policy.py) | `RequestPolicy` | 单轮物理请求计数、明确未发送时有限重试，摘要与聊天共用额度 |
| [llm/streaming.py](../adam/llm/streaming.py) | `sse_events`、`StreamReply` | 有界 SSE 解析、工具参数拼接、结束校验 |
| [cli/reply_output.py](../adam/cli/reply_output.py) | `ReplyOutput` | 正文增量、换行、去重及未完成提示 |
| [llm/client.py](../adam/llm/client.py) | `LLMClient` | 访问模型服务，将一次请求转换为模型消息和用量；模型消息也可能是工具请求 |
| [contracts/protocol.py](../adam/contracts/protocol.py) | `validate_assistant`、`validate_turn` | 定义合法助手消息和完整轮次的约束，检查工具请求与结果配对 |
| [session/storage.py](../adam/session/storage.py) | `SessionStore` | 管理会话文件、锁、读写与持锁恢复；先备份再原子替换，提供会话身份及已提交消息 |
| [session/edit_log.py](../adam/session/edit_log.py) | `EditLog` | 编辑状态纯投影；未开始计划恢复为 not_started，写入未知结果不猜测 |
| [session/log.py](../adam/session/log.py) | `SessionLog` | 解释日志事件，校验轮次、请求和工具关联，重建状态、有效历史及用量，规划并回放恢复事件；自身不访问文件 |
| [tool_calls/executor.py](../adam/tool_calls/executor.py) | `ToolExecutor`、`ToolBudget` | 编辑尝试计数、组织工具执行，将轨迹关联到轮次与请求，并形成回传模型的工具消息 |
| [tool_calls/registry.py](../adam/tool_calls/registry.py) | `ToolRegistry` | 提供模型可见的工具说明，校验参数、分派调用并统一结果格式 |
| [tool_calls/text_file.py](../adam/tool_calls/text_file.py) | `TextSnapshot`、`read_snapshot` | 同一次读取的完整字节、UTF-8 文本和版本 |
| [tool_calls/edit_tools.py](../adam/tool_calls/edit_tools.py) | `EditTools` | 规划前检查写入权限，批准后复查版本，以同目录临时文件替换；不提供跨程序事务 |
| [tool_calls/edit_plan.py](../adam/tool_calls/edit_plan.py) | `EditPlan`、`plan_edit` | 校验版本与唯一匹配，保留编码形式，生成候选内容和 diff；不写文件 |
| [tool_calls/file_tools.py](../adam/tool_calls/file_tools.py) | `FileTools` | 文件系统查询、搜索与读取能力 |
| [tool_calls/git_tools.py](../adam/tool_calls/git_tools.py) | `GitTools` | 固定范围的只读 Git 查询能力 |
| [tool_calls/command_tools.py](../adam/tool_calls/command_tools.py) | `CommandTools` | 表达待批准的命令计划，取得确认后运行程序，收集退出码、输出与执行结果 |
| [tool_calls/policy.py](../adam/tool_calls/policy.py) | `PathPolicy` | 表达并检查路径访问规则；配置值来自 config/settings |

## 错误与兼容边界

已知失败统一继承 AgentError：user_message 是受控提示，partial_reply 携带未保存回答。预算耗尽使用 BudgetError，模型通信使用 LLMError；CLI 只交给 terminal.show_failure 展示。存储 IO 在 storage 的操作边界转换为 StorageError 并保留 cause，命令日志失败继续停止本轮。应用层原样传播公共错误，不重复包装；程序错误和终端 OSError 继续传播，资源由组装入口释放。

- **交互与流程：** CLI 通过 AgentApplication 处理用户操作；`Conversation` 处理普通输入背后的会话流程。斜杠指令可以直接完成查询，不必请求模型。
- **流程与模型：** `Conversation` 决定何时请求、执行或停止；`LLMClient` 完成一次模型通信。一次用户输入可以触发多次模型请求。
- **历史与输入：** `SessionStore` 提供成功历史；`ContextBuilder` 组装系统提示、历史、当前轮次和工具说明。`Conversation.preview_context` 与实际请求使用同一构造逻辑，预览的当前轮次为空。terminal 只读取构造结果，不承担选择或预算决策。
- **摘要与循环：** `Conversation._prepare_context` 只在真实发送时按压力规划摘要；`TurnProgress` 记录本轮请求数和尝试状态。`CompactionService` 处理单次摘要、消费及提交；`CompactionLog` 只解释持久化状态。摘要后重新构造输入并检查预算；仅未发出的连接失败有限重试，每次独立操作，其他失败停止。
- **协议与日志：** `contracts/protocol.py` 检查消息结构和工具配对；`session/log.py` 检查持久化事件的关联，并识别完整提交的历史。它们不验证回答事实是否正确。
- **日志与文件：** `SessionLog` 解释事件含义；`SessionStore` 负责文件访问和持久化。失败轮次可以留下诊断记录，但不会因此进入模型的有效历史。
- **事实与表述：** Conversation 只记住本次运行最近轮次的标识，SessionStore 从同一份快照提取事实；恢复后的历史轮次仍可由存储接口读取。未知副作用不能因回答失败、重启或读取失败而推断为未执行。报告不进入模型历史，纯聊天不刷执行信息。
- **配置与执行：** 配置对象描述权限、额度和限制，各业务模块负责实际执行检查。
- **失败处理：** 可反馈的工具错误交回模型；命令日志无法可靠保存等错误会终止本轮。已知失败实现 AgentError 契约，CLI 委托 terminal.show_failure 展示；终端 IO 故障和意外程序错误不会被当成业务失败吞掉。

当前已有轮次、请求和工具关联留痕，以及独立的请求/轮次状态、结束原因与耗时。独立开发诊断与持锁中断恢复已实现，真实进程强杀验收已完成，不计划增加轮次诊断聊天命令。上下文构造、预览、完整轮次选择、手动摘要和自动压缩均已实现；流式响应已实现；部分输出仅用于展示，完整协议消息才返回业务循环。

## 公开错误契约

下面只展示与错误契约有关的源码依赖，箭头表示左侧模块导入右侧模块。它与上面的运行协作图视角不同；并非完整 import 清单。

```mermaid
flowchart TD
    CLI["cli/main.py"] --> ERROR["contracts/errors.py<br/>AgentError 公共契约"]
    CLI --> TERM["cli/terminal.py<br/>show_failure"]
    TERM --> ERROR
    CONV["application/conversation.py"] --> ERROR
    CONV --> OUTCOME["session/outcome.py"]
    OUTCOME --> ERROR
    STORE["session/storage.py"] --> OUTCOME
    LOG["session/log.py"] --> OUTCOME
    CONTEXT["context/builder.py"] --> ERROR
    CONFIG["config/loader.py"] --> ERROR
    LLM["llm/client.py"] --> ERROR
    STORE --> ERROR
    POLICY["tool_calls/policy.py"] --> ERROR
    COMMAND["tool_calls/command_tools.py"] --> ERROR
```

- AgentError 提供受控的 user_message、可选 partial_reply、稳定 code 与可选数值 http_status，不决定恢复或重试。展示层仍只使用前两个字段，不逐个匹配具体异常；ExecutionOutcome 将后两个字段转换为持久化结束信息。
- ConfigError、LLMError、StorageError、SessionNotFoundError、ToolError 和 CommandLogError 集中定义在 contracts/errors.py，调用方直接从定义模块导入。预算使用 BudgetError，回答未保存使用 TurnSaveError；新增业务错误类型无需增加 CLI 分支。
- 原始 HTTP 异常和响应错误由模型模块转换。原始 OSError 仅在 SessionStore 的实际文件/锁操作边界转为 StorageError，保留原因链且不直接展示系统异常文本。
- Conversation 在最终回答或其用量无法保存时转为 TurnSaveError，保留回答。ToolExecutor 将命令记录的存储或关联失败转为 CommandLogError，确保它不会被工具注册表当作可修正参数错误继续执行。
- 仅明确未发出的连接失败由 RequestPolicy 最多额外重试两次，每次独立记账；其他失败不因公共契约而自动重试。未识别的程序错误继续传播；终端输出自身的 OSError 也不会被误报为会话文件失败。open_application 的清理范围覆盖组件初始化、会话恢复与交互循环，初始界面或回答展示失败时均释放会话锁。
- 对公开存储接口做故障替身时使用 StorageError；模拟真实文件系统故障时在 IO 边界注入 OSError，验证实际转换、回滚和原因链。

公开异常契约与界面展示保持稳定。第 2、3 项在该契约上补齐状态、原因和耗时，V1.3.2 已通过自动化验收。组件组装和直接存储查询已分别移至 bootstrap 与 AgentApplication。

## 请求与轮次状态边界

- 每次模型请求前构造输入并检查累计消费、单次输入预算；输入估算包含工具 JSON Schema 及本轮新增结果。超限在 request_started 之前抛出 BudgetError，轮次记 failed / context_input_limit，不伪造请求记录或未知用量。
- 自动摘要沿用独立 compaction 事件，并通过 turn_id 关联触发轮次；摘要与聊天请求共同受每轮请求数和会话消费限制。预览没有副作用，旧工具缩减只作用于副本，只有提交完成的摘要才能替代对应历史。
- 开始事件建立 in_progress；request_finished 独立记录请求终态，和用量记录在同一块追加。响应有效而用量缺失时，请求可成功，整轮因预算统计不完整停止。
- 轮次成功只来自完整 message 块与 turn_completed 的成功提交；不能用独立状态事件跳过消息校验。turn_finished 只允许 failed / interrupted。
- Conversation 捕获失败后尽力保存终态，再传播原异常；KeyboardInterrupt / SystemExit 等退出信号记录 interrupted。失败日志本身出错只给原异常增加受控附注，不遮蔽最初失败。
- 原因与耗时通过 ExecutionOutcome 统一表达；Conversation 只负责在执行边界取单调时钟差，不在 CLI 增加错误类别分支。成功详情与完整消息同块保存，失败详情不含原始异常或远端正文。
- 新获会话写锁后，SessionStore 先备份原始字节，再用临时文件替换完整恢复结果；SessionLog 拒绝中间损坏，校验残留消息前缀并回放 session_recovered。完整物理记录不删除，未提交消息不成为历史。
- 恢复仅关闭缺少结束记录的操作，不推断真实请求结果、未知用量或未知命令结果，不自动重发。恢复失败保留原文件和原会话锁，重复重开不重复恢复。

## 文档维护

本文件统一维护当前架构图与职责边界。每次改动后的要求见 [AGENTS.md](../AGENTS.md) 的“架构文档同步”。测试中的组装辅助函数不属于生产架构。
