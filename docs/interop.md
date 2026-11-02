# C09.1 本机文本 Agent 互通

固定规范：[A2A 0.3.0](https://a2a-protocol.org/v0.3.0/specification/)。当前支持 JSON-RPC `message/send` 同步纯文本消息和 `/.well-known/agent-card.json` 能力发现；返回规范允许的直接 Agent Message。不是完整 A2A 实现。Task 生命周期、查询和取消留给 C09.2，生产工具审批接入留给 C09.3，固定参考服务验收留给 C09.4。

`adam.interop.client.TextAgentClient` 接收显式 `http://127.0.0.1:端口`；`discover()` 返回不可变 AgentCard，`send(text)` 返回不可变 TextResult。公共数据对象不携带 HTTP/供应商 SDK 对象。拒绝非回环、DNS 名称、凭据、路径和卡片目标切换；不继承代理、不跟随重定向、不自动重试。网络或响应失败不代表远端未执行。

`adam.interop.server.create_server(handler, port=0)` 返回显式宿主使用的标准库 HTTPServer；宿主负责 serve_forever、shutdown 和 server_close。服务只绑定回环地址，业务回调仅接收文本，不传 SessionStore、工具、凭据或本地批准。测试回调实际计算词数；无模型调用或费用。服务同步执行回调，不提供硬取消/隔离，适用于本机可信测试回调。它不具备网络部署认证，Card 不是认证证明。

当前限制：单消息文本不超过 16KiB，响应读取不超过 256KiB；只接受新消息和阻塞文本结果。不支持文件、流式、推送、持续上下文、Task 返回或重复提交去重；不会将这些能力标为已支持。远端文本仍是未独立验证的结果。

验收命令：`.cache/c-venv/Scripts/python.exe -m pytest tests/features/test_interop_text.py tests/test_architecture.py -q`。

2026-11-02：8项互通/失败场景与30项架构检查，共38项通过。测试启动独立服务进程和独立客户端进程，真实 GET Card、POST JSON-RPC，断言双方运行 PID 不同以及真实词数结果；补充业务异常、不支持的方法、非文本输入和非回环地址拒绝。服务通过 stdin 结束指令协作 shutdown，避免 Windows Python 启动器 PID 与解释器 PID 不同造成残留。未使用 MockTransport，不把本服务自测当作 C09.4 参考互操作。
