# Adam

V0 子集：Python 终端多轮聊天，支持 TOML 配置。历史仅保存在内存中，退出即清空；无文件工具、持久化或自动重试。

## 启动

需要 **Python 3.11+**。不要使用本机默认的 Python 2.7。

在项目根目录使用 uv：

```powershell
uv sync
Copy-Item config.example.toml config.toml
$env:DEEPSEEK_API_KEY = "你的密钥"
uv run python -m adam
```

没有 uv 时，可使用安装好的 Python 3.11+ 创建虚拟环境：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "httpx>=0.28,<0.29" "pytest>=8,<10"
Copy-Item config.example.toml config.toml
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m adam
```

`py -3` 需要本机已注册的 Python 3；否则用 Python 3 可执行文件的完整路径替换。只在首次配置时复制文件，避免覆盖已有配置。

## 配置与命令

`--config 路径` 可指定 TOML 文件，默认读取当前工作目录的 `config.toml`。

| 配置项 | 含义 |
| --- | --- |
| `llm.base_url` | API 根地址，例如 `https://api.deepseek.com`；程序追加 `/chat/completions`，兼容以 `/v1` 结尾的根地址 |
| `llm.model` | 默认请求模型 ID，示例为 `deepseek-flash` |
| `llm.timeout_seconds` | HTTP 操作超时，默认 120 秒；不是整个任务的硬截止时间 |
| `llm.system_prompt` | 系统提示词，空字符串表示不发送系统消息 |
| `llm.api_key_env` | 存放 API 密钥的环境变量名称 |
| `llm.api_key` | 直接填写 API 密钥；与 `api_key_env` 必须且只能配置一个 |

本地根目录 `config.toml` 已被 Git 忽略，示例配置不含真实密钥。使用其他文件名时需自行避免将密钥提交。

- 每行输入一个问题，同步等待完整回答。
- 启动时查询 `/models`，只显示服务端提供的模型名称，不显示请求 ID。名称缺失时显示“具体版本未知”，不推测具体版本；名称不等于固定快照保证。
- `/model`：查看当前模型及本次启动获取的模型名称列表。切换命令仍需输入服务商支持的 API 模型 ID，而非展示名称。
- `/model deepseek-v4-pro`：切换后续请求的模型，保留历史，仅本次运行有效，不写回配置。目录可用时拒绝未知 ID；目录不可用时允许切换并标记未验证。请求失败不会自动换回其他模型。
- 模型目录查询使用至多 5 秒的 HTTP 操作超时，失败仍可聊天；重新启动可刷新目录。
- `/new`：清空聊天历史，保留系统提示词。
- `/exit`、Ctrl+C 或输入结束：退出。
- 空输入跳过。
- 成功后才把本轮用户消息与助手回答加入历史。请求失败时此前历史保持不变，可以重新输入；不会自动重试。
- 无上下文裁剪，长对话超出服务限制时可用 `/new` 开始新对话。

## 代码与验证

- `adam/config.py`：TOML 加载与校验。
- `adam/llm.py`：HTTP 请求和响应校验。
- `adam/cli.py`：交互循环、内存历史和命令。
- `tests/test_chat.py`：使用假 HTTP 服务验证历史、失败、配置和退出；不会消耗模型费用。

```powershell
uv run pytest
# 或
.\.venv\Scripts\python.exe -m pytest
```

版本规划见 [docs/roadmap.md](docs/roadmap.md)。当前实现按后续确认的范围缩减了原 V0：不实现保存、列出、恢复会话。
