# B09 混合检索与可选精排

B09.1 已把 BM25 与本地向量召回接到同一条文档检索路径，以 RRF 融合并保留来源。B09.2 的可选精排和 B09.3 的 MRR、多相关片段 Recall@k 对照尚未完成。

## 配置与运行

```toml
[documents]
roots = ["docs"]
method = "hybrid"
hybrid_candidates = 20
rrf_constant = 60
semantic_min_score = 0.5
```

默认 method 仍是 keyword，改为 semantic 可仅运行向量；hybrid 才启用融合。hybrid_candidates 范围 1～20，实际每路候选数上限取它与本次 limit 的较大值；最终返回仍最多 limit 条。rrf_constant 为 1～1000 的整数，默认 60。

独立查询示例：

```powershell
python -m adam.document_search --workspace . --root docs --hybrid --limit 5 search "连接失败怎样重试"
```

`--semantic` 和 `--hybrid` 互斥；省略两者使用关键词。可用 `--hybrid-candidates` 和 `--rrf-constant` 做有界对照。`evaluate` 配合 `--hybrid` 会在同一题集分别运行 keyword、semantic、hybrid，当前仍用原 Hit@k 口径；新的多证据指标由 B09.3 增加。

混合与纯向量都使用已有本地 embedding。普通查询不会联网下载；先按 [文档检索](local-document-rag.md) 准备可选依赖及公开模型。没有准备模型时明确失败，不把关键词结果冒称为完整混合结果。

## 融合规则与依据

每次查询先成功刷新一份语料快照，两路均使用这批 chunk。关键词按 BM25 排序，向量候选先经现有余弦阈值过滤。对各路排名从 1 开始计算：

`RRF(chunk) = Σ 1 / (rrf_constant + 该路排名)`

两种原分数的尺度不同，因此不直接相加。一个片段在两路都靠前，会得到两个贡献；只被一路召回也可进入最终结果。按 chunk_id 去重，同路重复只计第一次、不占新名次；相同 ID 指向不同原文或版本时拒绝融合。同分按路径、起始行、片段 ID 稳定排序。

工具响应继续保存原文、路径、行号、源文件版本与 generation。混合结果额外包含：

- `score_kind=reciprocal_rank_fusion`：score 是排名融合值，不是概率或置信度。
- `fusion`：实际常量、候选上限和各路候选数。
- 每条的 `retrieval_ranks`：各召回方法、原排名和原得分，供核对融合依据。

原文从实际 chunk 返回，不用向量文本、模型解释或融合公式替代引用。两路最终合并前保留候选池，避免先把两路都截到最终 top_k 后错过同时被两路支持的片段。

## 权限、失败及验证

范围、读取授权、缓存原子写入、版本刷新和取消沿用 DocumentIndex / VectorSearch。任一路读取、向量格式、模型或缓存失败都停止该次检索；不会悄悄返回旧资料或把单路结果标为 hybrid。合法空候选仍可融合另一条成功路径，两路都空才返回无匹配；无匹配不证明现实中没有答案。

工具输出仍保留完整片段，在上限不足时整条省略，明确报告 output_limit；融合元数据也计入 JSON 字符上限。真实工具结束日志保存实际来源；离线重放使用冻结结果，不重新加载今日文档或 embedding。

[16 项业务场景](../tests/features/test_hybrid_retrieval.py) 覆盖候选池/去重/排名、独立单路、版本更新/删除、模型/缓存/维度/取消故障、空路、工具限额、实际工具与重放、配置和 CLI。向量生成边界使用受控替身，BM25、源文件、索引和缓存均真实执行；不据此宣称实际语义召回提升。连同文档/配置/架构相关共 128 项通过，Ruff 与 Pyright 通过，提交时执行全量项目门禁。
