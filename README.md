# Adam

Python 终端多轮聊天。每个会话一个 JSONL 文件，支持新建、列出、恢复与运行时模型切换。

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
- `--resume a3f9c821`：启动时恢复会话，精确匹配 8 位小写十六进制 ID。
- 不提供 `--sessions-dir`；旧 `--db` 和 `storage.db_path` 已移除。
- 两份配置缺失或无效会报错，不会静默采用其他文件。

## 聊天命令

- `/new [名称]`：创建并选择新会话，旧会话保留。
- `/sessions`：显示会话 ID、名称和当前会话标记。
- `/resume a3f9c821`：恢复指定会话并展示历史。
- `/model`：查看模型名称；`/model <模型ID>` 切换本次运行的模型。
- `/exit`、Ctrl+C 或输入结束：退出并释放会话锁。

命令由 `handle_command()` 统一处理；未知命令不会发送给模型。
模型目录不可用时仍可聊天；未知模型 ID 的处理沿用运行时提示。
首次正常输入时才自动创建会话；仅启动、列出和退出不会创建空会话。
同步、非流式调用；不自动重试，无上下文裁剪。

## JSONL 格式与写入边界

每行采用 `type`、UTC `timestamp`、`payload` 的统一结构。
第一行 `session_meta` 保存格式版本 1、ID、标题和系统提示词；
随后每条 `message` 保存 `turn_id`、`role` 和 `content`。
目录使用会话创建时的本地年月日；跨天续聊仍写原文件。
会话 ID 使用 8 位随机十六进制，创建时在根目录锁内检查冲突、独占创建文件。

模型完整回答后才追加用户和助手两行，共享同一个 turn_id。
写入会 flush/fsync，可捕获的写入错误尝试截断回写入前长度；文件系统写入不等同于数据库事务。
突然终止可能留下残缺行或半轮；加载时严格校验，损坏文件拒绝加载及继续追加。
先备份再手工修复，不静默丢弃记录。完整写入后、界面显示前崩溃的回答可能已经保存，恢复时以文件为准。
当前只接受完整的 user/assistant 配对；工具消息协议留待 V2 实现。

选中会话后持有操作系统文件锁，切换成功或退出时释放；切换失败保留原会话。
不同会话可分别使用；另一个进程选择被占用会话时立即报错。
`.locks` 中的锁文件可以保留，进程退出后锁会释放；不要在使用中删除锁文件。
所有写入者必须遵守同一锁协议；不要在运行时用编辑器修改会话文件。

旧 SQLite 数据不迁移、不删除，新版本不读写它们。

## 代码与验证

- `adam/config.py`：用户目录、TOML 加载与校验。
- `adam/storage.py`：JSONL 读写、格式校验和会话锁。
- `adam/conversation.py`：加载历史、请求模型、保存成功轮次。
- `adam/cli.py`：命令、输入循环和错误展示。
- `adam/llm.py`：HTTP 请求及响应校验。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pre_commit run --all-files
```

测试使用临时目录和假 HTTP，不调用付费模型；覆盖跨启动恢复、配置位置、
JSONL 损坏、写入故障回滚、会话隔离与跨进程锁释放。
下一步先做 V2.1 只读文件工具，再接工具循环；持久化执行状态延后。
规划见 [练手 TODO](docs/agent-practice-todo.md) 和 [roadmap](docs/roadmap.md)。
