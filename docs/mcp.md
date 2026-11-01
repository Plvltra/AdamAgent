# 本地 MCP：工具与资源

Adam 是客户端：发现工具、发送参数、读取结果。服务端负责实现查询并约束路径。
tools 是可调用操作；resources 是 URI 标识的资料；prompts 是可复用提示模板。E7 映射 tools，B15.1 已接入固定 URI 的文本 resources；prompts 留在 B15.2。
MCP 负责互通协议，不替代 Agent 的循环、额度、记录或批准，也不是操作系统沙箱。

依赖使用官方 Python SDK 2.3.0（uv.lock 固定），不是自行实现 JSON-RPC。
安装或更新依赖运行 uv sync --frozen。默认 [mcp] enabled=false，不启动进程。
E7.2 已接入工具目录。把示例配置中的整个 [mcp] 表替换为以下配置，并将程序/脚本路径改成实际位置：

~~~toml
[mcp]
enabled = true

[[mcp.servers]]
alias = "files"
command = 'G:/Myself/Adam/.venv/Scripts/python.exe'
args = ['G:/Myself/Adam/examples/mcp_readonly.py', '--workspace', '.']
tools = ["read_project_text"]
timeout_seconds = 10
~~~

服务工作目录是 Adam 的工作目录。模型看到 mcp_files_read_project_text；
参数仍只含 path，不能修改程序、工作根或允许列表。
示例服务只读根内的短 UTF-8 文本，拒绝越界、链接逃逸、凭据和超大文件。
服务器只读声明只是筛选条件；配置启用表示用户选择了可信程序，不是操作系统沙箱。
本版只给主 Agent 注册 MCP；子任务继续使用既有只读工具和 Skill，避免额外服务进程扩散。

Schema 使用 Draft 2020-12 校验嵌套对象、数组、联合类型等；只允许文档内部引用，
不会为解析 Schema 隐式访问外部 URL。启动/映射失败通过 ExitStack 回收已启动服务。
正常退出先关闭子任务，再关闭 MCP；模型或主 SessionStore 仍按原有生命周期关闭。

同一个应用复用一个服务连接；启动失败、断开或超时不自动重发。
取消后关闭连接并回收进程；服务端取消只是协议通知，客户端停止等待并不证明远端工作已停止，
因此本地资源验收实际检查了进程退出。
stdout 只供协议使用，服务日志写 stderr。客户端不把 LLM 密钥传给服务。

参考：[官方 Client](https://py.sdk.modelcontextprotocol.io/client/)、
[stdio 传输](https://py.sdk.modelcontextprotocol.io/client/transports/)、
[工具与只读声明](https://py.sdk.modelcontextprotocol.io/servers/tools/)、
[ClientSession](https://py.sdk.modelcontextprotocol.io/api/mcp/client/session/)。
调用使用 session.call_tool，避免高层 Client 在 HEADER_MISMATCH 后自动重发；
工具 is_error 必须转成失败，不能把正常收到协议响应等同查询成功。

## 验收与成本

工程验收运行：
~~~powershell
python -m pytest -q tests/features/test_mcp_connection.py tests/features/test_mcp_tools.py tests/features/test_mcp_failures.py
~~~

使用真实 SDK 服务子进程制造工具错误、主动退出、长等待和在途取消；确认进程退出，调用日志仅一次。
Schema 嵌套类型在客户端校验；服务另检查路径和凭据。名称冲突或非只读工具导致启动失败，
已经打开的服务由组装资源栈关闭。服务声明只读不能验证其实现，用户应配置可信程序。

从项目根运行可重现的只读性能对照：
~~~powershell
python -m scripts.compare_mcp --workspace . --path skills/verify-project/skill.toml --runs 3
~~~

输出冷启动、直接函数均值、连接复用均值、结果 JSON 字符数及内容一致性。
2026-10-29 在本机 Python 3.11.9 / MCP SDK 2.3.0 测量上述命令：冷启动 1.243 秒；
直接调用均值 1.46 毫秒，复用连接调用均值 29.27 毫秒；结果 JSON 分别为 196 / 477 字符，内容一致。
这不调用模型，token/模型费用为零；耗时与机器和缓存状态相关，不把一次测量当性能保证。
协议多出进程、序列化与传输开销；收益是可以独立替换服务实现，不需要修改 Agent 的工具循环。
简单本地函数仍适合直接调用，只有跨进程或独立扩展有价值时才使用 MCP。


## B15.1 资源目录与读取

资源是服务按 URI 提供的资料，不是 Adam 自行打开的本机路径或 URL。协议允许应用决定如何将资源用于上下文；Adam 将显式允许的 URI 映射为两个只读工具入口，仍走原发现、参数校验、额度、Hook、日志和重放。依据：[MCP resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)、[官方 SDK Client](https://py.sdk.modelcontextprotocol.io/client/)、[资源服务示例](https://py.sdk.modelcontextprotocol.io/servers/resources/)。实现使用 uv.lock 中的 SDK 2.3.0，不自行实现协议或旧版订阅。

可把上方服务配置中的 tools 改为空数组，并增加 resources；也可同时允许两类能力：

```toml
[mcp]
enabled = true

[[mcp.servers]]
alias = "files"
command = 'G:/Myself/Adam/.venv/Scripts/python.exe'
args = ['G:/Myself/Adam/examples/mcp_readonly.py', '--workspace', '.']
tools = []
resources = ["project://readme"]
timeout_seconds = 10
```

[教学服务](../examples/mcp_readonly.py) 将 project://readme 映射到工作目录的短 README.md，复用原服务的路径和大小限制（最多 16 KiB、3000 字符）。练习时使用含短 README 的临时项目。配置不自行创建资料、扩展权限或启动服务；只有 enabled=true 且实际打开应用才启动。仅配置 resources 的服务不发送 tools/list。URI 列表最多 16 项，必须是完整、互不重复且每项不超过 2048 字符的固定 URI；不展开模板或匹配通配符。

| 模型入口（alias=files） | 行为 |
| --- | --- |
| mcp_files_list_resources | 刷新允许范围内的目录，返回 server、uri、name、title、description、mime_type 与声明大小 |
| mcp_files_read_resource | 按 uri 读取文本；可传 expected_version，变化时拒绝返回新正文 |

按需工具模式下先发现这些入口，随后请求使用。目录最多四页、64 项；重复游标、重复 URI、缺少已配置资源或元信息超长会失败，不返回残缺目录。读取只接受所请求 URI 的 1～8 项文本，总正文 UTF-8 不超过 64 KiB；服务声明大小已超限时在请求前拒绝，实际响应仍重新检查。二进制、跨 URI 拼接和不完整结果拒绝。边界是在 SDK 接收响应后检查，不是整个服务或宿主的内存硬上限。

成功结果保留 server、原 uri、mime_type、文本和 size_bytes。version 是宿主对规范化的 URI/类型/文本列表计算的 SHA-256，version_source 明确为 content_sha256；它不是服务端发布版本，也不证明资料正确。expected_version 只保证这次实际读取匹配指定内容，不能保证来源此后不变。正文和资源描述不提升为系统指令；较长结果仍遵循原输出/附件规则。

低层 ClientSession 读取避免高层缓存及自动再次请求。目录页请求共享总等待窗口，读取超时、断开、格式错误、版本不符或取消后关闭并回收连接，不自动重试。模型不能选择服务程序、修改 URI 允许列表或借 URI 直接访问宿主。本阶段仍只向主 Agent 注册 MCP，原子任务范围保持。远程传输、非文本引用留在 B15.3。

[26 项业务场景](../tests/features/test_mcp_resources.py) 使用真实 SDK stdio 服务、文件、分页与取消；模型 HTTP 使用 MockTransport。覆盖目录过滤、版本变化、字节/条数限额、未授权 URI、无效配置、损坏分页、二进制拒绝、超时/断开无重试、真实进程回收、教学示例及完整聊天/离线重放。与原 MCP 工具、架构及重放共 72 项通过；教学文件的 Windows 换行预期已修正并单独复验。Ruff、Pyright 和链接检查完成，主线提交门禁运行全量 pytest；没有调用付费模型。
