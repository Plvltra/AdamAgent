# Adam

Python 终端 Agent，支持模型自主调用文件与 Git 查询工具，以及需逐次确认的通用命令。每个会话一个 JSONL 文件，支持新建、列出、恢复与运行时模型切换。

## 安装与配置

需要 Python 3.11+；使用项目虚拟环境，避免调用本机默认 Python 2.7。

```powershell
uv sync
$adamHome = Join-Path $HOME ".adam"
New-Item -ItemType Directory -Force $adamHome
# 仅首次配置时复制，已有配置不要覆盖。
Copy-Item config.example.toml (Join-Path $adamHome "config.toml")
Copy-Item llm.example.toml (Join-Path $adamHome "llm.toml")
$env:DEEPSEEK_API_KEY = "你的密钥"
uv run python -m adam
```

也可用 `.\.venv\Scripts\python.exe -m adam` 启动。
`ADAM_HOME` 可覆盖整个用户数据根目录；环境变量中的相对路径以启动目录为基准。
默认读取该目录中的 `config.toml`、`llm.toml`。

```text
~/.adam/
├── config.toml
├── llm.toml
└── sessions/
    ├── .locks/
    └── 2026/10/22/
        └── a3f9c821.jsonl
```

`config.toml` 管运行配置：

```toml
[storage]
sessions_dir = "sessions"

[chat]
system_prompt = "你是 Adam，一个友好的编程助手。"
```

`llm.toml` 的 `[llm]` 包含 `base_url`、`model`、`timeout_seconds`（默认 120），
以及二选一的 `api_key_env` / `api_key`。配置中的密钥不进入日志或 Git。
系统提示词只影响新建会话，恢复时使用会话文件里的原提示词。

- `--config 路径`：覆盖主配置位置，相对路径以启动目录为基准。
- `--llm-config 路径`：覆盖模型配置；未指定时读取主配置旁的 `llm.toml`。
- `storage.sessions_dir`：默认 `sessions`，相对路径以主配置目录为基准。
- `--workspace 路径`：指定工具工作目录，默认启动目录。
- `--resume a3f9c821`：启动时恢复会话，精确匹配 8 位小写十六进制 ID。
- 不提供 `--sessions-dir`；旧 `--db` 和 `storage.db_path` 已移除。
- 两份配置缺失或无效会报错，不会静默采用其他文件。

## 聊天命令

- `/new [名称]`：创建并选择新会话，旧会话保留。
- `/sessions`：显示会话 ID、名称和当前会话标记。
- `/resume a3f9c821`：恢复指定会话并展示历史。
- `/model`：查看模型名称；`/model <模型ID>` 切换本次运行的模型。
- `/usage`：查看当前会话累计 token 与上限；统计不完整时显示说明，不发起模型请求。
- `/exit`、Ctrl+C 或输入结束：退出并释放会话锁。

角色前缀使用加粗青色 `你>` 与加粗亮黄色 `Adam>`，正文保持终端默认颜色；恢复历史与未保存回答沿用同一样式。角色文字始终保留，避免仅靠颜色辨认。
颜色自动用于交互终端；重定向输出、`TERM=dumb` 或设置非空 `NO_COLOR` 环境变量时使用纯文本。颜色仅用于显示，不进入会话 JSONL 或模型消息。Windows 控制台由 Colorama 启用兼容显示。

命令由 `handle_command()` 统一处理；未知命令不会发送给模型。
模型目录不可用时仍可聊天；未知模型 ID 的处理沿用运行时提示。
首次正常输入时才自动创建会话；仅启动、列出和退出不会创建空会话。
同步、非流式调用；不自动重试，无上下文裁剪。

## JSONL 格式与写入边界

每行采用 `type`、UTC `timestamp`、`payload` 的统一结构。
第一行 `session_meta` 保存格式版本 4（不兼容 v1/v2/v3）、ID、标题和系统提示词；
随后每条 `message` 保存 `turn_id` 和完整协议 `message`，包含 role/content、tool_calls、tool_call_id 及模型实际返回的 reasoning_content。
目录使用会话创建时的本地年月日；跨天续聊仍写原文件。
会话 ID 使用 8 位随机十六进制，创建时在根目录锁内检查冲突、独占创建文件。

用户提交时先写 `turn_started`，保存 turn_id 与原始输入。本轮所有 request_started/request_usage 关联这个 turn_id。
每次工具处理前写 tool_started，返回后写 tool_finished，关联 turn_id、request_id、tool_call_id，保留原始参数与实际回传结果。命令另保留 command_* 事件，记录确认和进程边界。
模型给出最终回答后追加本轮完整消息链，并以 `turn_completed` 标记结束，沿用输入时生成的 turn_id。
恢复时校验轮次、请求与工具关联，成功消息中的工具调用和结果须与执行记录一致。失败或达到限制的轮次仍保留诊断记录，但不写入有效聊天历史。
只有完整消息块及 turn_completed 写入后才算可恢复的成功对话；request_usage 仅表示用量结算，tool_finished 仅表示取得工具结果，不保证业务成功。
写入会 flush/fsync，可捕获的写入错误尝试截断回写入前长度；文件系统写入不等同于数据库事务。
突然终止可能留下残缺行或半轮。通过 `/resume <ID>` 或 `--resume <ID>` 新取得写锁后，先在原目录备份为 `<ID>.jsonl.<随机值>.bak`，再修复残缺尾行并追加 session_recovered。
完整记录保留，未提交消息不进入历史；完整 JSON 仅缺换行时补齐，中间损坏或非法完整事件仍报错。只读列表/诊断不执行修复；需要时直接用文件名中的 ID 恢复。
备份或替换失败保留原日志；完整写入后、界面显示前崩溃的回答可能已经保存，恢复时以文件为准。
`request_started` 和 `request_usage` 独立记录每次模型请求和用量；失败任务的已知用量也累计。
未结算的请求、缺失用量和超时会使预算状态变为未知，恢复后不允许继续调用模型，需新建会话。
旧 v1/v2/v3 文件明确报错，不迁移、不删除；请新建 v4 会话。旧文件仍会使 `/sessions` 列表报错：可自行归档到会话目录之外，或将 `storage.sessions_dir` 配置为新的目录。

选中会话后持有操作系统文件锁，切换成功或退出时释放；切换失败保留原会话。
不同会话可分别使用；另一个进程选择被占用会话时立即报错。
`.locks` 中的锁文件可以保留，进程退出后锁会释放；不要在使用中删除锁文件。
所有写入者必须遵守同一锁协议；不要在运行时用编辑器修改会话文件。

旧 SQLite 数据不迁移、不删除，新版本不读写它们。

本版完成关联留痕、第 2 项请求/轮次状态和第 3 项结束原因与耗时。request_finished 记录请求的成功、失败或中断；turn_finished 记录轮次失败或中断，轮次成功仍须完整消息和 turn_completed 提交。请求成功不保证整轮成功，用量未知也不自动等于请求失败。终态记录稳定原因码、受控错误说明和单调时钟毫秒耗时；独立开发诊断已实现；持锁恢复和真实进程强制终止验收已完成，不新增轮次诊断聊天命令。事件说明和验收步骤见 [会话执行轨迹](docs/session-tracing.md)。

## 代码与验证

模块架构图与职责边界见 [当前系统架构](docs/architecture.md)。

- `adam/config.py`：用户目录、TOML 加载与校验。
- `adam/storage.py`：JSONL 读写与会话锁。
- `adam/session_log.py`：事件格式与轮次、请求、工具关联校验。
- `adam/conversation.py`：加载历史、请求模型、保存成功轮次。
- `adam/cli.py`：命令、输入循环和错误展示。
- `adam/terminal.py`：角色标签、颜色、纯文本退化及统一失败展示。
- `adam/errors.py`：公开业务失败契约，包含可展示说明、未保存回答及稳定原因码。
- `adam/outcome.py`：请求与轮次共用的结束信息及字段校验。
- `adam/diagnostics.py`：`python -m adam.diagnostics <JSONL路径>` 输出只读 JSON 报告，无需模型配置。
- `adam/llm.py`：HTTP 请求及结构化助手响应、用量解析。
- `adam/tool_calls/`：工具注册与参数验证、文件工具、Git 工具、权限与资源限制。
- `adam/tool_calls/executor.py`：所有工具的执行协调与独立轨迹记录。
- `adam/protocol.py`：助手工具请求与完整轮次的协议校验。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/features -v
.\.venv\Scripts\python.exe -m pre_commit run --all-files
```

已知业务失败统一遵循 `AgentError`，CLI 仅通过公共契约展示提示和未保存回答。额度停止使用“执行受限”提示，模型通信或响应失败使用“模型请求错误”提示；底层失败不会因此自动重试。原始存储 IO 错误在存储边界转换，终端自身故障和意外程序错误继续传播。

业务功能点测试集中在 `tests/features/`，按会话、启动配置、工具循环、执行轨迹、终端交互和命令执行组织，见 [功能测试导航](tests/features/README.md)。基础校验保留在 `tests/test_config.py`、`tests/test_llm_config.py`、`tests/test_storage.py`、`tests/test_llm.py` 和 `tests/test_tools.py`。
自动化使用临时目录和假 HTTP，不调用付费模型；覆盖跨启动恢复、配置位置、JSONL 损坏、写入故障回滚、会话隔离与跨进程锁释放。假 HTTP 验证工程流程，不代替真实模型的选工具和回答效果评估。`tests/features/test_process_crash.py` 另用本地 TCP 服务与独立进程验证强杀：请求中、消息写入中、完整提交后三个边界均经过全新进程恢复。
V2.1/V2.2 基础工具闭环已实现，V2.3 固定评估尚未完成。V1.3 已实现关联留痕、请求/轮次状态、结束原因与耗时；独立开发诊断、持锁中断恢复和真实进程强制终止验收已完成；下一主线是 V2.3 固定任务评估，再进入 V3 上下文管理。JavaScript 编排不是必做前置。
规划见 [练手 TODO](docs/agent-practice-todo.md) 和 [roadmap](docs/roadmap.md)。


## 只读工具与循环

工具模块提供 `list_directory`（分页列目录）、`find_files`（glob文件查找）、
`read_file`（按行读取）、`search_text`（字面搜索）、`file_info`，以及
`git_status`、`git_diff`（含 staged 参数）、`git_log`（最多50条）。
模型看到 JSON Schema；参数必须满足类型、必填字段与额外字段约束。
未知工具、坏参数和文件错误作为 `role=tool` 的 JSON 错误返回，模型可修正。
同一响应的多个工具按顺序执行，每个返回对应 tool_call_id；超出工具预算则整批不执行。

```powershell
uv run python -m adam --workspace G:\Myself\Adam
# 输入：找出模型配置在哪里加载，读取代码后解释错误处理，并给出路径与行号。
# 输入：查看当前 Git 改动，概括改了什么。
```

相对工具路径以 workspace 为基准；绝对路径须在 workspace 或用户配置的
`permissions.additional_read_dirs` 内。请求路径和解析后的真实路径都检查授权。
搜索默认跳过 `.git`、`.venv`、`node_modules` 与缓存目录；本版使用固定跳过规则，
不解释 `.gitignore`。忽略目录中的文件可用明确路径读取；目录联接/符号链接不递归。
`.env*`、`llm.toml`、常见私钥和凭据目录另行拒绝，不能靠软链接绕过。
这是应用层控制，不是 OS 沙箱，也不能识别所有文件里的秘密；工具内容会发送给模型。

默认每次读取200行、单次结果8000字符、扫描5000项、扫描文件最大1MiB。
超长行会截断，结果标记 truncated；分页结果包含继续位置。搜索最多50条匹配。
Git需要普通仓库根目录，不支持worktree或外部共享对象目录；禁用外部diff/textconv、
fsmonitor和hooks，并拒绝配置了过滤器的status/diff。Git diff检查已跟踪文件的路径权限，
仓库包含被禁止读取的文件时拒绝diff。Git通过参数列表调用，无Shell拼接；每个查询有超时，
输出写临时文件后限量读取，不把无限输出载入内存。Git提交说明本身仍可能包含用户写入的敏感文字。

## 预算

`config.example.toml` 包含全部默认配置。每个用户任务最多8次模型请求、16次工具调用，
单次模型输出最多4096 token；每个session累计50000 token，包含每次重复发送的历史。
这些值可在 `[agent]` 修改。达到阈值不再调用；最后一次请求可能超过阈值，
它不是严格的账单封顶，也不对应固定人民币额度。
请求有用量但回答无效时仍计入；没有可靠用量则持久标记未知并停止。
已知预算耗尽可以提高配置额度后重启，未知预算不能通过提高额度消除。
终端显示执行的工具名称，默认不逐次打印用量；输入 `/usage` 查看会话累计值。隐藏显示不影响持久记账、额度限制或超限提示。

自动化验证使用假 HTTP 和临时仓库，不消耗真实模型额度。用户已反馈通用命令成功、拒绝与失败的真实模型回答；固定任务评估和完整日志核对仍待完成。
参考：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、
[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。


编码与注释维护约定见 [AGENTS.md](AGENTS.md)。工具权限配置位于 `[permissions]`，
资源限制位于 `[tool_limits]`，字段与默认值参见 `config.example.toml`。
`scan_timeout_seconds` 在遍历目录时检查，不中断阻塞的文件 IO；
`git_timeout_seconds` 限制每个 Git 子进程，一次工具调用可能运行多个查询。
Git 路径清单大小由 `max_git_path_list_bytes` 单独限制。


## 通用命令工具

`run_command(argv, cwd, timeout_seconds)` 接受程序和参数列表，支持脚本、Git、构建等命令。
每次展示完整执行计划，由用户输入 y 确认。返回退出码、有界输出、超时和耗时，独立保存执行记录。
使用说明见 [通用命令工具](docs/command-tools.md)。
