# B05 压缩保真样本与评估

B05.2 已提供固定样本和只读引用审计；B05.3 的模型查询对照尚未实现。本阶段没有运行真实模型，没有摘要质量分数，也没有证明新提示优于旧提示。

## 同一历史，不同问题

[样本 v1](../examples/compaction-evaluation/cases-v1.json) 包含一段 20 轮合成历史：早期项目约束、配置读取、认证命令失败、重试原因、索引损坏和取消动作，中间加入无关材料，末尾保留两轮近期消息。所有任务共享这一份历史，避免不同资料造成无法归因的差异。

| 维度 | 查询 | 必须保留的区别 |
| --- | --- | --- |
| 事实 | 模型配置位置；连接和读取超时 | llm.toml 的路径；10 秒与 60 秒不能互换 |
| 原因 | 401 为什么不能反复重试；文件存在为何仍读取失败 | 过期凭据；checksum 不符，而非文件缺失或权限问题 |
| 否定约束 | 是否可以删日志；是否可以标记完成 | “不要删除”；“测试通过才能完成” |
| 失败路径 | 认证命令是否成功；迁移是否做完 | 工具 ok=true 不代表退出码 1 成功；取消并未执行 |

每个判据保存结论要求、维度、`turn_id:message_index` 和逐字依据。加载时校验这段依据确实存在于指定原文，并验证消息协议、工具配对、重复键/ID、版本及字段；错误样本直接拒绝。文件上限 1 MiB，源 SHA-256 随列表和后续评估传递。

这些是评估答案，不发给摘要模型。历史里的命令只是合成文本，列出或审计不会执行。

## 只读入口

在仓库根目录使用项目 venv：

```powershell
python -m scripts.compaction_cases
python -m scripts.compaction_cases --case preserve-logs
python -m scripts.compaction_cases --case config-location --summary summary.txt
```

summary.txt 是已有摘要文本。入口只读样本和指定摘要，将 JSON 报告打印到终端，不调用模型、不写会话或改动输入文件。自定义样本通过 `--cases` 显式指定。

[audit_summary](../scripts/compaction_cases.py) 去重解析 `[turn_id:message_index]`，返回真实消息角色、摘要、哈希和截断标记。未知轮次、越界索引、格式错误不会映射到附近原文。调用者提供实际 Summary 的 covered_turn_ids 后，还会拒绝不在覆盖范围内的引用；命令行只知道文本，报告 coverage_checked=false，不能声称已核对实际请求资料。

报告同时给出每个判据的原文、期望说明、引用是否出现和逐字短语是否出现。这两个布尔值都只是观察；semantic_status 始终为 not_reviewed。例如“原话是不要删除日志，但可以忽略它”包含正确原文和引用，结论仍然错误；“请保留日志”可能语义正确，即使没有逐字匹配。

## 验收

[27 项业务测试](../tests/features/test_compaction_fidelity.py) 覆盖四类样本、原文副本隔离、正确引用、否定词丢失、矛盾、改写和遗漏、虚构/越界/未覆盖引用、重复引用、非法样本、有界读取及只读 CLI。测试不请求模型；人工语义判读与真实模型对照在下一步实现。

相关说明：[任务导向压缩](task-aware-compaction.md)、[完整路线](agent-book-practice-plan.md)、[架构](architecture.md)。
