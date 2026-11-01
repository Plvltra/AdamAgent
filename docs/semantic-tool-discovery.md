# B16 语义工具发现

用途：模型想“查看修改记录”，目录可以按用途找出 git_log 等候选，而不要求自然语言与名称逐字相同。候选仍需通过原 discover_tools 选择，下一次模型请求实际收到完整 Schema 后才能调用。检索本身不执行、不批准、不扩展读取范围。

## B16.1 已实现的基础

[ToolDirectory](../adam/tool_calls/discovery.py) 为冻结的可信注册目录增加 version：完整说明、参数 Schema、分组、能力和注册顺序共同决定 SHA-256。更改参数但保留名称与摘要，也会改变版本；它不是工具执行结果的版本。

[SemanticToolRanker](../adam/tool_calls/semantic_discovery.py) 接收目录、当前模式、查询和可选分组，返回 ToolRanking。先按原能力规则和分组过滤，再给宿主注入的本地 EmbeddingBackend 编码工具名称、分组、160字符摘要；完整参数不交给编码器。查询上限200字符，向量复用原 unit_vector 的有限值、维度与非零检查。

ToolRanking 包含实际策略、目录版本、候选、可选模型身份与分数。语义模式使用余弦分数和阈值，准确名称匹配优先且不因低分消失；同分维持注册顺序。空字符串查询与默认 keyword 仍使用原目录顺序和子串规则，分组限制一直有效。

缓存仅在内存中保存最近一次允许范围，最多 max_index_tools 项；目录、模式、分组或编码器身份变化均重新编码。整个批次及查询向量成功校验后才替换缓存，推理过程中模型身份改变也拒绝使用。没有新增索引文件或外部数据库。

目录超限时返回 keyword_fallback_v1 / candidate_limit；模型缺失、失败或坏向量返回 keyword_fallback_v1 / embedding_unavailable，并清除该次失败缓存。降级按完整允许目录做原关键词匹配，不截掉部分候选后声称完整语义搜索。原因是固定码，不暴露底层异常正文。正常语义无匹配没有降级原因；Ctrl+C 直接传播。

## 配置与接入阶段

ToolDiscoveryConfig 与 TOML 已校验以下选项；B16.1 只提供目录 API，现有聊天仍使用原 list_tools，B16.2 才接入语义排序和本地模型适配器。当前不会因为写入 ranking 而下载或请求任何模型。

```toml
[tool_discovery]
mode = "on_demand"
ranking = "keyword"
embedding_model = "BAAI/bge-small-zh-v1.5"
max_index_tools = 256
semantic_min_score = 0.5
```

mode 控制完整工具说明何时加载；ranking 独立控制目录如何排序，两者不能互相代替。模型名称沿用现有文档/记忆默认本地模型；数量允许1～1024，阈值0～1，布尔值不能冒充数字。默认关键词不加载编码器。真实本地语义召回及开销留在 B16.3 对照，不把固定测试向量当模型效果。

## 验证

[22项业务场景](../tests/features/test_semantic_tool_directory.py) 通过真实 ToolRegistry/ToolVisibility 检查过滤先于编码、候选不选择/不执行、准确名称与分组、Schema/模型/模式/分组缓存失效、坏批次/失败降级、取消与配置。连同原发现/聊天/配置/架构共136项通过（16.34秒）；默认模型名改为沿用现有本地模型后，相关69项再次通过。最初测试夹具误用了不存在的能力枚举，纠正为已有 EDIT_FILE 后重跑通过，未修改生产权限规则。

Ruff、Pyright、链接和新增/改写注释单独审查；主线提交门禁执行全量 pytest。B16.2 接入与 B16.3 二十工具效果对照尚未完成。
