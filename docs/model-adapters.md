# B24 最小模型协议适配

沿用已有 `ModelClient` 和 JSONL 消息格式。只实现文本和函数工具的两种协议：已有 Chat Completions，以及非流式 Messages；不建设通用路由和运行中热切换。

- [x] B24.1：公共消息、工具身份和原字段视图。
- [ ] B24.2：双协议客户端、TOML入口和安全切换限制。
- [ ] B24.3：本地工具循环、切换及失败验收。

[message_view](../adam/contracts/model_history.py) 提取角色、正文、工具ID、函数名、JSON参数和结果关联。原字段保存为独立副本，`to_raw()`可还原，包括`reasoning_content`及未知扩展；不会修改既有JSONL。视图只校验单条消息，原有完整轮次校验仍负责工具配对。

能力声明描述本版适配范围，不替服务端作承诺。Chat Completions沿用现有JSON/SSE及`reasoning_content`；Messages首版计划仅支持非流式文本/工具，不启用思考块或多模态。目标协议遇到不能移植的字段应明确拒绝，不静默删除，也不重新执行已有工具。

B24.1的[6项测试](../tests/features/test_model_protocol_view.py)验证工具身份、原始字段往返、可变数据隔离和内容边界；连同架构28项通过。Ruff、Pyright及新增说明已检查。第二协议尚未接入，本阶段不声称能请求Messages。真实服务商调用和回答效果未验证，不自动调用付费API。

[系统架构](architecture.md) 与 [路线](agent-book-practice-plan.md) 同步。后续按用户最新要求完成最小闭环即可。
