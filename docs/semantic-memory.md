# B06 语义记忆

B06.1 已完成本地语义召回接入和离线工程验收。[B06.2](memory-identity.md) 已接入主体、生效起点及原子替代关系；综合消歧和真实召回效果留给 B06.3。向量测试替代推理，只证明程序流程，不证明真实模型理解正确。

## 使用与范围

默认保留关键词策略。需要语义召回时，在用户 config.toml 的现有 `[memory]` 配置中设置：

```toml
method = "semantic"
embedding_model = "BAAI/bge-small-zh-v1.5"
semantic_min_score = 0.5
max_semantic_records = 256
```

独立的 max_entries、max_input_tokens 及总上下文预算继续有效；只选择完整记录，工具循环固定本轮快照，子 Agent 只继承已选记录。`/context full --query 问题` 可预览实际资料和降级警告；命令不会发起聊天模型请求，语义模式会做本地推理。

复用文档检索的 FastEmbed CPU 后端、分窗口编码和模型缓存。缓存默认位于 ADAM_HOME/documents/models；自定义 documents_dir 时随该目录变化。需要安装可选依赖并手动准备模型时使用既有入口：

```powershell
uv sync --extra documents
python -m adam.document_search --model BAAI/bge-small-zh-v1.5 prepare-model
```

自定义目录需为准备命令显式传 `--cache-dir`。只有该准备命令允许下载公开模型；聊天检索设置 local_files_only，不上传记忆、不自动下载、不消耗聊天 API 额度。

MemoryService 先读取当前项目及用户明确保存的全局记录，再排除删除和过期版本，最后才交给编码器。其他项目不进入向量输入；文件读取授权不会扩大记忆范围。语义相似度仅决定相关性排序，不代表事实正确率，也不能授予工具权限。

## 缓存、降级与来源

SemanticMemoryRanker 只在内存缓存本次范围内的有限向量；模型身份、范围、ID、版本或正文改变时重新计算。重启后重新生成，不建设第二个持久化记忆库。按相似度降序排序，同分按更新时间和 ID 稳定排序。

有效候选超过 max_semantic_records（默认 256，上限 1024）、问题超过 1000 字符，或模型缺失、向量非法及推理失败时，整批退回现有关键词排序并提示原因。不会只处理前一部分资料后声称全量语义召回。取消继续向上传播；记忆存储故障仍显示 unavailable，会话保存和请求记账错误仍停止执行。

请求快照的 strategy 区分 keyword_v1、semantic_v1 和 keyword_fallback_v1。新策略的 retrieval 保存模型身份或固定降级原因，以及实际选中 ID 的相似度；完整原文、版本、范围和来源仍在 records。没匹配、预算容不下和故障各自保留状态，分数不会写入给模型的事实正文。

旧关键词记录保持原格式；新记录严格校验来源字段、分数和 ID 是否对应。录制同时冻结实际选择，离线重放不重新加载记忆、计算 embedding 或访问网络。旧录制缺少语义配置仍可读取。

## 验收

[12 项业务场景](../tests/features/test_semantic_memory.py) 覆盖范围先过滤、全局及过期/删除、版本和模型变化、输入预算、故障降级、超限不推理、取消、禁止自动下载及实际请求/录制重放。[17 项契约场景](../tests/test_semantic_memory.py) 覆盖 TOML、旧录制配置与损坏检索证据。连同原记忆、重放、架构和文档向量相关回归共 117 项通过；提交门禁执行全量 pytest。

注释审查：共享 EmbeddingBackend 和 unit_vector 的原说明随实现迁到 contracts/embedding.py，原导入入口保留；MemoryService 的失败契约扩展为显式语义降级。没有删除仍适用的存储、权限或预算说明。架构见 [系统架构](architecture.md)，任务见 [完整路线](agent-book-practice-plan.md)。
