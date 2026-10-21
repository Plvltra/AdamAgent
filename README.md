# Adam

V1 会话持久化：Python 终端多轮聊天，TOML 配置，SQLite 自动保存成功轮次，支持列出、新建和恢复会话。

## 启动

需要 **Python 3.11+**。不要使用本机默认的 Python 2.7。

在项目根目录使用 uv：

```powershell
uv sync
Copy-Item config.example.toml config.toml
Copy-Item llm.example.toml llm.toml
$env:DEEPSEEK_API_KEY = "你的密钥"
uv run python -m adam
```

没有 uv 时，可使用安装好的 Python 3.11+ 创建虚拟环境：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "httpx>=0.28,<0.29" "pytest>=8,<10"
Copy-Item config.example.toml config.toml
Copy-Item llm.example.toml llm.toml
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m adam
```

`py -3` 需要本机已注册的 Python 3；否则用 Python 3 可执行文件的完整路径替换。只在首次配置时复制文件，避免覆盖已有配置。

## 配置与命令

`--config 路径` 指定主配置，默认 `./config.toml`；模型配置默认读取主配置同目录的 `llm.toml`，可用 `--llm-config 路径` 覆盖。两份文件必须存在，内容无效时启动报错。

`config.toml` 保存 `[storage]` 和 `[chat]`；`llm.toml` 保存 `[llm]`。`storage.db_path` 默认 `data/adam.db`，相对路径以主配置目录为基准；`--db` 优先于该配置，其相对路径以启动工作目录为基准。

旧配置迁移：将原文件的 `[llm]` 放到 `llm.toml`，把 `system_prompt` 移到主配置的 `[chat]`；参照两份示例文件。已有会话仍使用数据库内保存的提示词。

| 配置项 | 含义 |
| --- | --- |
| `llm.base_url` | API 根地址，例如 `https://api.deepseek.com`；程序追加 `/chat/completions`，兼容以 `/v1` 结尾的根地址 |
| `llm.model` | 默认请求模型 ID，示例为 `deepseek-flash` |
| `llm.timeout_seconds` | HTTP 操作超时，默认 120 秒；不是整个任务的硬截止时间 |
| `chat.system_prompt` | 系统提示词，空字符串表示不发送系统消息 |
| `llm.api_key_env` | 存放 API 密钥的环境变量名称 |
| `llm.api_key` | 直接填写 API 密钥；与 `api_key_env` 必须且只能配置一个 |

本地根目录 `config.toml` 和 `llm.toml` 已被 Git 忽略，示例配置不含真实密钥。使用其他文件名时需自行避免将密钥提交。

- 每行输入一个问题，同步等待完整回答。
- 启动时查询 `/models`，只显示服务端提供的模型名称，不显示请求 ID。名称缺失时显示“具体版本未知”，不推测具体版本；名称不等于固定快照保证。
- `/model`：查看当前模型及本次启动获取的模型名称列表。切换命令仍需输入服务商支持的 API 模型 ID，而非展示名称。
- `/model deepseek-v4-pro`：切换后续请求的模型，保留历史，仅本次运行有效，不写回配置。目录可用时拒绝未知 ID；目录不可用时允许切换并标记未验证。请求失败不会自动换回其他模型。
- 模型目录查询使用至多 5 秒的 HTTP 操作超时，失败仍可聊天；重新启动可刷新目录。
- `/new [名称]`：新建并切换会话，使用当前配置的系统提示词；旧会话仍保留。
- `/sessions`：列出会话 ID 和名称，星号表示当前会话。
- `/resume <ID>`：恢复会话，展示已保存消息；后续请求使用该会话保存的系统提示词和历史。
- `/exit`、Ctrl+C 或输入结束：退出。
- 空输入跳过。
- 模型成功返回后，以事务保存本轮用户消息与助手回答。请求失败不保存本轮；保存失败会展示未保存的回答，不会自动重发模型请求。
- 无上下文裁剪，长对话超出服务限制时可用 `/new` 开始新对话。

## 代码与验证

### 会话存储与恢复

`adam/storage.py` 提供 `SessionStore`；`adam/conversation.py` 组织加载、请求与保存，终端通过它使用 SQLite 历史。
存储使用 Python 标准库 `sqlite3` + SQLite，`sessions` 保存标题和系统提示词；`messages` 通过外键关联会话，
以会话内的 `position` 排序，保存角色和内容。同一会话的位置不可重复。

```python
from pathlib import Path

from adam.storage import SessionStore

with SessionStore(Path("chat.db")) as store:
    session_id = store.create_session("学习", "请使用中文")
    store.append_turn(session_id, "什么是事务？", "一组一起成功或回滚的数据库操作。")

with SessionStore(Path("chat.db")) as store:
    session = store.get_session(session_id)
    messages = store.load_messages(session_id)
    print(session.title, [(message.role, message.content) for message in messages])
```

父目录应由调用方创建。`with SessionStore(...)` 负责关闭数据库连接；不使用 `with` 时需调用 `close()`。
`append_turn` 内部开启写事务，再读取位置、插入用户和助手消息，成功提交、异常回滚。
网络调用不应位于该事务中。`sqlite3.Error` 数据库异常向上传递，不自动重试；不存在的会话抛出 `SessionNotFoundError`。
这里只保存成功轮次。待完成状态与模型选择持久化尚未实现；恢复会话不会恢复上次进程的模型选择，继续使用当前运行配置及 `/model` 的选择。

新增测试位于 `tests/test_storage.py`：空会话、关闭重开、会话隔离、第二次插入失败回滚、
会话不存在。回滚测试用临时数据库触发器制造真实错误，不连接 LLM。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storage.py -v
```

- `adam/config.py`：TOML 加载与校验。
- `adam/llm.py`：HTTP 请求和响应校验。
- `adam/cli.py`：交互循环、启动参数和命令。
- `tests/test_chat.py`：使用假 HTTP 服务验证历史、失败、配置和退出；不会消耗模型费用。

```powershell
uv run pytest
# 或
.\.venv\Scripts\python.exe -m pytest
```

版本规划见 [docs/roadmap.md](docs/roadmap.md) 和 [练手 TODO](docs/agent-practice-todo.md)。


### 跨启动续聊示例

默认数据库为配置文件所在目录下的 `data/adam.db`。第一次输入问题时自动创建会话；
仅启动、查看列表、退出不会创建空会话。退出后不会自动选择历史会话，需要显式恢复。

```powershell
.\.venv\Scripts\python.exe -m adam
# 输入 /new 学习数据库，然后聊天；用 /sessions 查看 ID，再 /exit
.\.venv\Scripts\python.exe -m adam --resume 1
```

也可启动后输入 `/resume 1`。指定其他数据库：

```powershell
.\.venv\Scripts\python.exe -m adam --db data/custom.db --resume 1
```

相对 `--db` 路径相对于当前工作目录；跨启动须使用同一数据库。
数据库父目录自动创建，文件及 SQLite 附属文件已被 Git 忽略。旧版纯内存会话无法追溯恢复。
此版本面向单进程交互使用；同一会话多进程同时生成回答的协调尚未实现。

`tests/test_persistence.py` 使用真实临时 SQLite、模拟 HTTP 和输入验证完整链路，包含
两次启动入口后继续聊天、会话切换隔离、恢复不存在会话、写入失败不污染上下文。

### sqlite3 存储实现

`SessionStore` 集中管理参数化 SQL，业务层通过方法读写数据。
`Session` 和 `Message` 是不可变 dataclass，查询结果转换为这些对象返回。
每个存储实例持有独立连接，通过 `with SessionStore(...)` 关闭；不跨线程共享实例。
建表和整轮消息写入使用显式事务；`BEGIN IMMEDIATE` 保证分配消息位置前取得写锁。
保持原有表结构，已有会话文件可直接使用，无需迁移。
测试覆盖旧数据库读取、关闭重开后的约束、整轮回滚和多连接并发追加。
