# B24 最小模型协议适配

沿用已有 `ModelClient` 和 JSONL 消息格式。只实现文本和函数工具的两种协议：已有 Chat Completions，以及非流式 Messages；不建设通用路由和运行中热切换。

- [x] B24.1：公共消息、工具身份和原字段视图。
- [x] B24.2：双协议客户端、TOML入口和安全切换限制。
- [ ] B24.3：本地工具循环、切换及失败验收。

[message_view](../adam/contracts/model_history.py) 提取角色、正文、工具ID、函数名、JSON参数和结果关联。原字段保存为独立副本，`to_raw()`可还原，包括`reasoning_content`及未知扩展；不会修改既有JSONL。视图只校验单条消息，原有完整轮次校验仍负责工具配对。

能力声明描述本版适配范围，不替服务端作承诺。Chat Completions沿用现有JSON/SSE及`reasoning_content`；Messages首版仅支持非流式文本/工具，不启用思考块或多模态。目标协议遇到不能移植的字段应明确拒绝，不静默删除，也不重新执行已有工具。

B24.1的[6项测试](../tests/features/test_model_protocol_view.py)验证工具身份、原始字段往返、可变数据隔离和内容边界；连同架构28项通过。Ruff、Pyright及新增说明已检查。B24.1只完成视图；B24.2已接通下述请求链路。真实服务商调用和回答效果未验证，不自动调用付费API。

[系统架构](architecture.md) 与 [路线](agent-book-practice-plan.md) 同步。后续按用户最新要求完成最小闭环即可。

## B24.2 配置与最小闭环

在llm.toml中设置`protocol="messages"`、`stream=false`、`base_url="https://api.deepseek.com/anthropic"`；模型和密钥沿用原字段。默认协议仍为`chat_completions`，旧配置行为不变。协议在启动时确定，改配置后重启；没有运行中热切换，也不因失败自动换协议。Messages模式的模型列表仅展示配置值，不表示已查询远端目录。

[LLMClient](../adam/llm/client.py)按配置调用[Messages传输](../adam/llm/messages_client.py)，[协议转换](../adam/llm/messages_protocol.py)将原工具ID对应到tool_use/tool_result。同批结果保持顺序，放入紧随助手的user消息；实际工具执行、批准、预算和JSONL仍是原流程。原系统提示只允许在历史开头，中途system消息明确拒绝。私有推理或未知扩展不移植，联网前返回已知零用量，原历史不修改；建议另建会话。首版显式关闭thinking，不支持图片、思考块和服务端工具。

请求为一次非流式POST，连接未建立沿用原有限重试类别，HTTP失败和未知消费不自行重试。截断或未知块拒绝提交。输入总量包含非缓存输入、缓存读取与创建三项，任一缺失保持未知；本版未输出独立缓存细项。前缀观测仍是原会话字典的本地观察，不等同于远端缓存命中。

协议依据：[DeepSeek Messages API](https://api-docs.deepseek.com/api/create-message/)、[兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)，2026-11-02核对。只用MockTransport替换远端；真实文件读取、工具执行、JSONL和重启恢复已跑通。11项新增、协议/工具/流式/配置/架构相关121项通过（10.28秒）；Ruff和Pyright通过。新增模块和改动注释已审查，complete原契约保留并补充协议范围；架构同步。主线按 AGENTS.md 三级验证规则执行。跨协议恢复、取消和未知消费恢复留B24.3，不宣称真实模型质量已验证。
