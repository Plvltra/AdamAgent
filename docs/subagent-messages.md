# B28 运行中主子消息

- [x] B28.1：父→子有序邮箱，绑定run/message ID，在安全点消费。
- [x] B28.2：子→父有界进展与收集，双向状态持久化与配额。
- [ ] B28.3：双子任务、取消/结束竞态及重启不重发验收。

## B28.1 父到子

新增主模型工具`message_agent(agent_id, run_id, message_id, text)`，给当前主会话中的指定活运行补充要求。沿用`spawn_agent`/`list_agents`返回的run_id；`send_message`仍只负责已结束子Agent的新一轮续聊。运行结束、取消、封口或run_id过期时明确拒绝，绝不隐式启动新任务。独立审查者拒绝运行中补充。

SubagentManager在启动子线程前建立该run_id专属EventQueue，最多32条待消费消息、每轮64个唯一消息、每条8192字符。相同message_id及正文重试返回同一序号且duplicate=true，内容冲突拒绝；不占额外并发/新建槽，但父工具调用、子模型请求及token仍受原额度限制。

子Conversation由宿主绑定input_owner=run_id，在请求前、完整工具批次之后消费AGENT_MESSAGE。消息以带run/message标识的user资料形式进入上下文，明确是主Agent补充而非用户新增授权，不能扩大原权限。最终回答前原子封口；有补充则继续模型请求，轮次结束关闭邮箱。子Agent没有message_agent工具，不能借此横向通信或再次委派。

accepted仅表示入队，不保证已经消费或执行。原父工具调用轨迹记录参数与回执，成功子历史保存消费正文；B28.2已补充独立邮箱快照和状态结算，见下节；真实强杀与竞态验收留在B28.3。

验收：模拟HTTP+真实父子线程完整流程通过（3.43秒）：运行中补充、重复重试只消费一次、两次连续子运行及旧run消息拒绝。旧会话续聊、子权限与原用户安全点5项通过（4.68秒）。定向Pyright与Ruff通过；未调用付费模型。
## B28.2 双向进展与持久状态

辅助子模型获得`report_progress(message_id, text)`，接收者由宿主绑定，不能指定其他父/子Agent。最多64条，每条2048字符；同ID同正文重试去重，不同正文拒绝。审查者仍不注册此工具。父模型用`collect_agent_progress(agent_id, run_id, cursor=0)`每次读取一条，按next_cursor继续，过期或其他运行ID不能读到新运行内容。预览按原工具回传上限裁剪并标明truncated，完整正文保留在快照。

进展只是子模型报告，不代表已验证成功。收集进展不会改变原最终结果的交付标记，主模型仍必须wait_agents后才能结束。

`RunMailbox`复用EventQueue并把双向事实写入`_subagents/<parent_session>/mailboxes/<agent_id>/<run_id>.json`：父消息accepted/in_context/unprocessed，子进展reported/collected。collected只表示宿主读取，不推断主模型已处理。父消息保留原父请求/tool_call来源，成功子历史仍保留正文标记。接受与确认回调在队列变更前保存，失败不回传成功回执；父子线程通过实例锁串行写快照，fsync后原子替换。邮箱容量、原模型/工具额度并行约束，不额外请求模型。

运行结束关闭邮箱，将未消费消息标为unprocessed；重启在持有主会话锁时根据已恢复子日志/运行索引结算邮箱，不恢复执行队列、不重发。旧进展仍可按原run读取；损坏身份、顺序、重复ID或超限快照报错。每文件最多4MB。主/子原文件与资料工具保护内部_subagents目录，读取进展经专用接口；批准的宿主通用命令仍不是沙箱。

验收：双向真实线程/模拟HTTP闭环及保存失败、去重、终态未消费与恢复读取通过；与B28.1共3 passed（5.72秒）。旧续聊、权限和架构24 passed（5.78秒）；补充恢复ID校验后2项复验2.63秒。定向Pyright与Ruff通过。真实进程强杀、两子任务及取消竞态留给B28.3。