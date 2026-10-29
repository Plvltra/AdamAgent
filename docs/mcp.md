# E7 本地 MCP

Adam 是客户端：发现工具、发送参数、读取结果。服务端负责实现查询并约束路径。
tools 是可调用操作；resources 是 URI 标识的资料；prompts 是可复用提示模板。本版只映射 tools。
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
