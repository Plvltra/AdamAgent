# 知识目录与二层概览

## B11.1 当前能力

[catalog.py](../adam/documents/catalog.py) 从 DocumentIndex 已授权、已限额的 Corpus 构造两层结构：主题概览 → 全部原片段引用。B11.1 建立独立 API，B11.2 已接入下述概览和详情查询入口；不读取额外文件，不调用聊天或 embedding 模型。

主题按分块标题归一空白、忽略大小写合并，没有标题时使用文件名。相同标题可以合并多个文件，但这只是索引规则，不证明两篇资料的论点一致。主题 ID 绑定规范化名称，目录版本绑定来源快照、全部引用、概览内容及限制。

每个引用保留 chunk ID、文件路径、文件内容版本、行号和原文字符数。概览优先选择不同文件的非标题片段，再补其他片段；每段只摘原文前缀，记录块内字符区间与是否截断。所有片段引用仍完整保留，不把代表摘录宣称为完整总结。

默认最多 200 个主题、10,000 个引用；每主题概览最多 600 字符、3 个来源片段。主题或引用总量超过限制时整份拒绝，不能默默漏掉资料；概览的局部截断则显式标记。调用者可传 CatalogLimits 调整有界限制。

KnowledgeCatalogStore 在文件锁内校验旧快照，按当前来源与规则生成目录，用原子替换保存变化。源更新、删除或规则变化会生成新版本；重复片段和同文件混合版本拒绝。缓存损坏保留原文件并报错；取消或保存失败也不返回旧概览冒充当前资料。访问原文时仍须重新核对当前授权，缓存引用本身不授予读取权限。

## 验收

[12 项业务场景](../tests/features/test_knowledge_catalog.py) 使用真实临时文件与 DocumentIndex，覆盖跨文件主题、逐字概览、原文不变、总量与摘录限制、源修改/删除、规则变化、重启缓存、关系损坏、保存失败、取消、混合版本、重复身份和空目录；连同架构检查共 33 项通过。提交前运行 Ruff、Pyright 和全量 pytest。

本阶段没有生成式语义总结，也没有真实模型质量结论。B11.2 已提供入口，B11.3 再对照全库概览与单点查询，验证过时或缺失引用不能被当作当前证据。

## B11.2 概览到原文

[KnowledgeDirectory](../adam/documents/catalog_query.py) 每次查询都先重新扫描当前已授权文档，刷新目录后校验调用者持有的版本。来源、范围或概览规则变化时旧版本拒绝，要求重新取得概览；读取失败不退回旧缓存。结果描述本次读到的快照，其他程序仍可能在返回后修改文件。

配置 documents.roots 后，与 search_documents 一起注册两个普通只读工具；没有文档源时不注册，注册本身不读取资料：

| 工具 | 用途 | 参数与返回 |
| --- | --- | --- |
| knowledge_overview | 浏览主题或搜索标题及代表摘录 | query 可空；返回主题 ID、catalog_version、代表摘录及各自原文位置、完整引用数 |
| read_knowledge_topic | 读取选中主题的实际原片段 | topic_id、catalog_version 必填；返回逐字正文、文件版本和行号 |

两者都支持 offset 与 limit（1～20），结果给 next_offset / has_more。概览续页也必须带版本并保持 query；返回的是连续完整条目，容量不足时不拆正文、不跳过未返回条目。连首项都放不下时明确 output_limit，需提高宿主 max_result_chars，反复使用相同参数不会获得更多内容。元数据本身超限直接报错。

概览明确 coverage=representative_excerpts_only，file_count/topic_count/reference_count 描述语料范围，不能当作已读覆盖率。按概览关键词没找到不等于原文没有答案；单点问题继续使用 search_documents。同名主题中的跨文件资料也可能冲突，结论须核对实际原文。

独立入口不加载聊天配置，不下载模型：

```powershell
python -m adam.document_search --workspace . --root docs catalog
python -m adam.document_search --workspace . --root docs catalog "network"
python -m adam.document_search --workspace . --root docs --limit 3 read-topic <topic_id> <catalog_version>
```

模型工具沿用原模式、Hook、工具数和上下文预算、主/子来源权限及持久化日志；没有新路径参数或额外授权，暂不注册并行执行适配器。录制保存当时返回的正文和目录版本，离线重放使用记录，不读取已经变化的资料。

[16 项新场景](../tests/features/test_knowledge_queries.py) 覆盖分页完整性、跨文档、更新/删除/撤销权限、非法编码、取消、范围收窄、输出限额、参数先行校验、未知主题、独立 CLI、只读注册及模拟 HTTP → 真实概览 → 原文 → 日志重放。加上 B11.1、现有文档工具和架构共 55 项通过。提交门禁继续执行全量 pytest。
