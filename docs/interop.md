# C09.1 本机文本 Agent 互通

固定规范：[A2A 0.3.0](https://a2a-protocol.org/v0.3.0/specification/)。当前支持 JSON-RPC `message/send` 同步纯文本消息和 `/.well-known/agent-card.json` 能力发现；返回规范允许的直接 Agent Message。不是完整 A2A 实现。Task 生命周期、查询和取消留给 C09.2，生产工具审批接入留给 C09.3，固定参考服务验收留给 C09.4。

`adam.interop.client.TextAgentClient` 接收显式 `http://127.0.0.1:端口`；`discover()` 返回不可变 AgentCard，`send(text)` 返回不可变 TextResult。公共数据对象不携带 HTTP/供应商 SDK 对象。拒绝非回环、DNS 名称、凭据、路径和卡片目标切换；不继承代理、不跟随重定向、不自动重试。网络或响应失败不代表远端未执行。

`adam.interop.server.create_server(handler, port=0)` 返回显式宿主使用的标准库 HTTPServer；宿主负责 serve_forever、shutdown 和 server_close。服务只绑定回环地址，业务回调仅接收文本，不传 SessionStore、工具、凭据或本地批准。测试回调实际计算词数；无模型调用或费用。服务同步执行回调，不提供硬取消/隔离，适用于本机可信测试回调。它不具备网络部署认证，Card 不是认证证明。

当前限制：单消息文本不超过 16KiB，响应读取不超过 256KiB；只接受新消息和阻塞文本结果。不支持文件、流式、推送、持续上下文、Task 返回或重复提交去重；不会将这些能力标为已支持。远端文本仍是未独立验证的结果。

验收命令：`.cache/c-venv/Scripts/python.exe -m pytest tests/features/test_interop_text.py tests/test_architecture.py -q`。

2026-11-02：8项互通/失败场景与30项架构检查，共38项通过。测试启动独立服务进程和独立客户端进程，真实 GET Card、POST JSON-RPC，断言双方运行 PID 不同以及真实词数结果；补充业务异常、不支持的方法、非文本输入和非回环地址拒绝。服务通过 stdin 结束指令协作 shutdown，避免 Windows Python 启动器 PID 与解释器 PID 不同造成残留。未使用 MockTransport，不把本服务自测当作 C09.4 参考互操作。


## C09.2 任务生命周期

新增 `TaskClient.submit(local_id, text)`、`query(local_id)`、`cancel(local_id)`，返回不可变 RemoteTask（源地址、本地关联、远端 task/context ID、状态、文本产物）。宿主可保存快照并用同源客户端 `attach` 恢复。首个发送响应丢失时远端 ID 未知，关联仍保留并拒绝再次派发；规范没有按 messageId 查询的标准方法，因此此情况不会伪造可恢复性。已知远端 ID 的查询失败保留最后已知状态，恢复后查询同一 ID。相同本地关联不能再次 submit，包括终态及未知状态。

宿主显式注入 TaskService 后，回环服务支持非流式 `message/send` Task 返回、`tasks/get` 与 `tasks/cancel`。服务端生成 ID，完成结果存为文本 artifacts；失败返回 failed；取消是协作信号，回调退出前仍为 working，退出后才发布 canceled。取消不撤销已发生副作用，也不是原生强杀。服务端任务暂存内存、默认最多64个，不支持服务进程重启恢复或多轮 continuation；宿主负责先停止 HTTP 接收再关闭 TaskService。客户端恢复不等于服务端持久化。保持 streaming=false、pushNotifications=false。

2026-11-02：新增5项真实HTTP场景：成功与客户端恢复、取消/失败终态、查询响应超时后按原ID恢复、首次发送响应丢失且禁止重复、异源恢复拒绝；与原双进程文本场景共13项通过，Ruff/Pyright通过。延迟注入的是服务响应边界，任务与回调真实运行；断连后断言实际业务只执行一次。本批未跑全量或重复提交门禁。C09.3 原工具/审批与 C09.4 固定参考服务尚未实现。


## C09.3 主 Agent 工具与批准

显式调用 `build_application(..., remote_policy=RemoteAgentPolicy(...), confirm_remote=...)` 后，主工具目录注册 remote_submit、remote_query、remote_cancel；默认不启用，子任务不继承。端点、报价依据和预算由宿主注入，模型只提供本地关联与文本。原发现/固定请求名单、模式、Hook、工具调用预算和 JSONL 仍生效。submit/cancel 为 EXTERNAL_ACTION（plan 拒绝），query 仅查询本会话原关联。提交和取消分别批准准确源/正文或任务；回调缺失默认拒绝。本地批准不发送为远端身份或权限，远端仍自行约束业务回调。

费用以整数 microUSD 表示（1000000 = 1 USD）。宿主必须给出每任务约定总价上限（含查询/取消）、会话金额上限、任务数量上限和报价来源；未知单价不得编造，应先取得可信服务约定再启用。每个原 remote_submit 开始记录保守占一个名额及该报价，不因拒批、失败或重启退还；上限检查发生在批准与网络之前。源/合同变化时旧快照恢复拒绝。**报价上限是宿主与服务的约定，不能约束不守约的远端计费；首版只用于已知本机服务。实际费用未上报始终为 null/not_reported，不把零报价当作实测零费用，也不混入主模型 token 用量。**

返回证据包含真实工具 source、远端 task/context ID、状态、费用合同和 independently_verified=false。已有 JSONL 的真实远端工具结果恢复关联，其他工具返回内容不能伪造关联；未完成或丢失结果保留未知，禁止再次派发。大结果仅省略远端文本，优先保留ID和不确定性。取消在本地HTTP请求前检查，等待中的HTTP最长使用原10秒网络超时；应用退出不隐式取消已批准的远端任务，恢复后可查询/显式批准取消。

2026-11-02：6项原主应用+真实回环HTTP场景：完成/日志/实际费用未知/重启查询不重发、拒批、plan拒绝、报价上限、任务数/会话隔离、取消单独批准；与架构共36项通过，Ruff/Pyright通过。仅模型HTTP使用MockTransport，远端服务、回调与本地存储真实运行；无真实付费模型。文档和新增契约已审查，未删除原组装入口说明。C09.4 固定参考服务互操作仍待验。
