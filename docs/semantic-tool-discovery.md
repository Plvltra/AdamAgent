# B16 语义工具发现

用途：模型想“查看修改记录”，目录可以按用途找出 git_log 等候选，而不要求自然语言与名称逐字相同。候选仍需通过原 discover_tools 选择，下一次模型请求实际收到完整 Schema 后才能调用。检索本身不执行、不批准、不扩展读取范围。

## B16.1 已实现的基础

[ToolDirectory](../adam/tool_calls/discovery.py) 为冻结的可信注册目录增加 version：完整说明、参数 Schema、分组、能力和注册顺序共同决定 SHA-256。更改参数但保留名称与摘要，也会改变版本；它不是工具执行结果的版本。

[SemanticToolRanker](../adam/tool_calls/semantic_discovery.py) 接收目录、当前模式、查询和可选分组，返回 ToolRanking。先按原能力规则和分组过滤，再给宿主注入的本地 EmbeddingBackend 编码工具名称、分组、160字符摘要；完整参数不交给编码器。查询上限200字符，向量复用原 unit_vector 的有限值、维度与非零检查。

ToolRanking 包含实际策略、目录版本、候选、可选模型身份与分数。语义模式使用余弦分数和阈值，准确名称匹配优先且不因低分消失；同分维持注册顺序。空字符串查询与默认 keyword 仍使用原目录顺序和子串规则，分组限制一直有效。

缓存仅在内存中保存最近一次允许范围，最多 max_index_tools 项；目录、模式、分组或编码器身份变化均重新编码。整个批次及查询向量成功校验后才替换缓存，推理过程中模型身份改变也拒绝使用。没有新增索引文件或外部数据库。

目录超限时返回 keyword_fallback_v1 / candidate_limit；模型缺失、失败或坏向量返回 keyword_fallback_v1 / embedding_unavailable，并清除该次失败缓存。降级按完整允许目录做原关键词匹配，不截掉部分候选后声称完整语义搜索。原因是固定码，不暴露底层异常正文。正常语义无匹配没有降级原因；Ctrl+C 直接传播。

## 配置与接入阶段

ToolDiscoveryConfig 与 TOML 校验以下选项。B16.1 建立独立目录 API，B16.2 已把它接入聊天 list_tools 和本地模型适配器；启用后只在非空目录查询时推理，不会自动下载模型。

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

Ruff、Pyright、链接和新增/改写注释单独审查；主线提交门禁执行全量 pytest。B16.2 接入已在下节记录；B16.3 二十工具效果对照尚未完成。


## B16.2 接入原工具发现

设置 ranking="semantic" 后，[ToolDiscovery.list_tools](../adam/tool_calls/discovery_tools.py) 使用任务描述查候选，结果增加 ranking.strategy、directory_version、embedding、fallback_reason，语义候选各带 score。默认 keyword 保持原返回结构。查询仍受原分页和输出限制，连同元信息计算大小；缩小页仍放不下一项时明确失败，不截断掉 next_offset。

bootstrap 为主任务和每个子任务分别注入 FastEmbedBackend，模型缓存使用实际 documents_dir/models，缺省 documents_dir 为 sessions_dir/_documents。后端延迟加载，allow_download=False；组装和 /context 预览不推理。查询找不到模型时明确返回关键词降级，不能把它当成功的语义排序。

准备模型使用已有显式入口：先安装可选 documents 依赖，再执行 python -m adam.document_search --cache-dir <实际documents_dir> --model <embedding_model> prepare-model。该命令可以下载公开模型文件；正常聊天不会替你执行。使用已有同名模型缓存可以直接复用，不需要另建向量数据库。

list_tools 只返回候选，discover_tools 才加入本轮选择，后续请求实际附上完整 Schema 才允许调用。同批 discover+新工具调用仍被拒绝。路径、模式、Hook 和逐次批准继续由原执行器检查；语义分数不参与授权。主子实例和向量缓存分开，子任务只对自己实际注册且当前模式允许的目录排序。

排序来源随原工具结果进入 JSONL 和录制；每次请求继续记录实际 tool_schema.names/utf8_bytes。重开会话保留这些证据，离线重放使用当时的目录与结果，不重新加载 embedding 模型或外部工具。目录更新后重新组装冻结快照及版本，旧索引不会作为新目录结果；本版没有进程内热更新注册表。

[8项接入场景](../tests/features/test_semantic_tool_flow.py) 验证真实 list→discover→后续请求→文件读取、同批提前调用拒绝、plan 过滤与越界文件拒绝、编码失败降级、分页/结果过小、变更目录、真实子线程隔离及重启/零编码重放。与原发现、目录基础、架构、重放共108项通过（27.49秒），Ruff/Pyright通过。语义向量和远端聊天为测试替身，本地文件与执行器真实运行；真实二十工具效果留在 B16.3。主线提交门禁运行全量 pytest。
