# B25 流式空闲与总时限

- [x] B25.1：计时基础、有效增量标记和TOML配置。
- [ ] B25.2：接入HTTP读取与已有错误/取消收尾。
- [ ] B25.3：本地HTTP的心跳、慢输出和断流验收。

`llm.semantic_idle_seconds`默认120秒，`stream_total_seconds`默认600秒，必须为有限正数。前者限制多久没有有效模型增量，后者限制整个流式请求持续多久；原`timeout_seconds`仍描述HTTP操作超时。非流式Messages不使用这两项监控。

[StreamWatch](../adam/llm/stream_watch.py)使用单调时钟。收到字节只增加网络计数，不能延长空闲期限；有效输出可重置空闲时间，不能重置总时间。等于期限即过期，迟到输出不能恢复请求。它本身不启动线程，不能中断尚未返回的阻塞系统调用，实际读取边界和HTTP超时配合由B25.2完成。

[StreamReply](../adam/llm/streaming.py)成功解析一次事件后给出`progressed`：非空正文、推理、工具ID/名称/参数增量算进展；角色、空增量和独立usage通知不算。原字节上限、结束状态和工具格式校验不变。非法事件直接失败，调用者不读取其进展标记。SSE心跳本来就不形成模型事件。

[8项测试](../tests/features/test_stream_watch.py)使用可控时钟验收计时，经过原StreamReply验收增量，实际读取TOML验证配置；相关94项通过（7.20秒），Ruff/Pyright检查。新增源码/测试及原注释差异已核对。此阶段没有改变HTTP停止行为，不能称已完成请求超时闭环；后续使用真实本地HTTP补验，不调用付费模型。

[架构](architecture.md)与[完整路线](agent-book-practice-plan.md)同步，主线按 AGENTS.md 轻量验证规则执行。
