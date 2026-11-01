# B05 压缩保真样本与评估

B05.2 的固定样本和引用审计、B05.3 的模型对照入口与人工判读报告已实现。本阶段只运行模拟 HTTP 工程验收；真实摘要质量仍待显式评估，没有证明任务导向摘要更好。

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

[audit_summary](../scripts/compaction_cases.py) 去重解析 `[turn_id:message_index]`，返回真实消息角色、原文片段、哈希和截断标记。未知轮次、越界索引、格式错误不会映射到附近原文。调用者提供实际 Summary 的 covered_turn_ids 后，还会拒绝不在覆盖范围内的引用；命令行只知道文本，报告 coverage_checked=false，不能声称已核对实际请求资料。

报告同时给出每个判据的原文、期望说明、引用是否出现和逐字短语是否出现。这两个布尔值都只是观察；semantic_status 始终为 not_reviewed。例如“原话是不要删除日志，但可以忽略它”包含正确原文和引用，结论仍然错误；“请保留日志”可能语义正确，即使没有逐字匹配。

## 同一历史的两组对照

[运行入口](../scripts/compaction_eval.py) 复用生产 CompactionService.plan：generic 不提供当前问题，focused 提供所选查询；两组使用相同历史、提示、模型、输出上限与覆盖轮次，只有任务定位资料不同。覆盖不一致时在请求前拒绝比较。每个问题各请求一次，两组顺序交替；默认比较 config-location、auth-cause，共计划四次摘要请求，可用重复的 `--case` 选择 1～8 个查询。

每次运行必须使用新目录。程序先复制样本、导入专用合成历史并保存所有请求计划，再调用摘要模型；历史工具调用不会执行。sources 中的会话标题明确标注合成资料，零消费仅指这些夹具，不指真实摘要请求。评估摘要不会提交为聊天摘要，也不会触发后续任务。

需要真实模型时手动运行：

```powershell
python -m scripts.compaction_eval run .local/compaction-run-01 --llm-config "$HOME/.adam/llm.toml" --allow-paid-api
python -m scripts.compaction_eval report .local/compaction-run-01
python -m scripts.compaction_eval review-template .local/compaction-run-01 | Set-Content -Encoding utf8NoBOM review.json
python -m scripts.compaction_eval report .local/compaction-run-01 --review review.json
```

最后两条保存和读取人工判读表；`utf8NoBOM` 写法适用于 PowerShell 7。报告与模板入口只读，不加载模型配置或联网。完整请求保存在编号 JSON 中，终端报告仅给出文件名、输入哈希、摘要、原文依据和用量，避免重复打印长历史。配置密钥及远端错误正文不保存。

默认总量上限 50,000 token，可显式设置 `--max-known-tokens`。每次请求前检查已消费加输入估算、余量和输出预留；样本较长，默认额度可能让后面的项目保持 planned，不能据此声称四次均完成。服务端实际消费也可能超过估算；未知用量、失败、保存故障或取消停止后续请求，不自动重试。报告分别显示 planned、started、succeeded、failed、interrupted，以及 missing/invalid；未知消费不补成零。

## 人工判读与证据

[只读报告](../scripts/compaction_report.py) 核对样本哈希、计划顺序、固定资料、覆盖范围、输入指纹和用量。模板只为成功摘要生成判据，默认 not_reviewed。阅读报告中的原文和摘要后，将判据标为 preserved（保留）、omitted（遗漏）、contradicted（曲解）或 unclear（无法确定），并填写简短 note。未完成判读保持 not_reviewed。

评审绑定运行及摘要哈希；摘要被替换、判据错配、重复评审或非空结论没有依据时拒绝。按组汇总人工结论和未评审数量，不自动把逐字命中转换为质量分。mock/live 标记保留；人工评价模拟响应也不能成为真实模型效果证据。本实验只比较摘要保真，未验证压缩后实际任务成功率。

## 验收

[27 项样本测试](../tests/features/test_compaction_fidelity.py) 覆盖四类判据、原文副本隔离、正确引用、否定词丢失、矛盾、改写和遗漏、虚构/越界/未覆盖引用、重复引用、非法样本、有界读取及只读 CLI。

[28 项对照测试](../tests/features/test_compaction_evaluation.py) 覆盖同一历史不同问题、真实 JSON/SSE 解析、实际用量与预算、失败/取消/保存故障停止、只读报告、人工遗漏判读、过期评审及损坏资料拒绝。连同 B05.1 共 64 项通过；没有调用付费 API。提交门禁继续执行全量 pytest。

相关说明：[任务导向压缩](task-aware-compaction.md)、[完整路线](agent-book-practice-plan.md)、[架构](architecture.md)。
