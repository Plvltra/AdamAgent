# B09 混合检索与可选精排

B09.1 已把 BM25 与本地向量召回接到同一条文档检索路径，以 RRF 融合并保留来源。B09.2 已接入默认关闭的本地精排及明确降级。B09.3 已完成 MRR、多相关片段 Recall@k，并保存 [真实十二题四路对照](retrieval-evaluation.md)。

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

`--semantic` 和 `--hybrid` 互斥；省略两者使用关键词。可用 `--hybrid-candidates` 和 `--rrf-constant` 做有界对照。`evaluate` 配合 `--hybrid` 会在同一题集分别运行 keyword、semantic、hybrid，同时报告 Hit@k、MRR@k、宏/微 Recall@k 和无答案误召回；开启 --rerank 会另加精排路线，保留原融合基线。

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

## B09.2 可选本地精排

`RerankBackend` 只接收查询与候选文本并返回对应分数，不返回新文档或来源 ID。默认 `rerank=false`；只有 hybrid 方法可以启用，普通关键词/向量路径保持原行为。

```toml
[documents]
roots = ["docs"]
method = "hybrid"
rerank = true
reranker_model = "BAAI/bge-reranker-v2-m3-int8"
rerank_candidates = 20
rerank_query_chars = 256
rerank_passage_chars = 512
```

程序先产生融合候选池，再精排，最后截取本次 limit。精排候选数取配置与 limit 的较大值，最多 20 个；两路召回池也至少覆盖这个数量。查询前缀上限 1～1000 字符，单片段标题加正文前缀上限 1～4000 字符。输入窗口只影响打分，返回的引用仍是实际原始片段。

`FastEmbedReranker` 使用已有可选 FastEmbed 依赖的 TextCrossEncoder，延迟加载本地 ONNX、单 CPU 线程；没有新增在线服务。模型文件独立保存在文档缓存的 rerankers 子目录。准备和查询：

```powershell
python -m adam.document_search prepare-reranker
python -m adam.document_search --workspace . --root docs --hybrid --rerank search "连接失败怎样重试"
```

首次准备可下载公开模型；普通检索始终 local_files_only，不下载、不上传资料。可用 `--reranker-model` 显式换模型。单个推理调用运行期间不提供强制杀线程；每个有界候选前后检查取消，取消仍终止本次工具，不降级成成功。

输出里的 `score` 始终保留 RRF 值与 retrieval_ranks；成功精排另附 rerank_score，并以 `order=reranker` 标记实际次序。得分只表示相关性，不是答案概率。reranking 报告记录后端身份、候选数、输入上限、前缀是否被截以及实际状态；模型分词器仍可能进一步截断，因此 input_policy 明确为 prefix_then_model_tokenizer，不宣称模型已读完原文。

任何候选产生非法数量、非数值、NaN/无穷分数，或模型依赖/加载/推理失败时，整次精排回退到未改动的融合次序。已得到的部分分数全部丢弃，不让一半候选受新规则、一半受旧规则排序。报告为 status=fallback、order=fusion，并附受控失败码；原文、版本、来源和融合排名保持。空候选为 empty，不加载精排模型；文档源/索引故障仍整体失败，不在精排阶段掩盖。

精排报告也占工具输出预算。若元数据本身已放不进上限，明确报错；不会为保留一个片段而静默删除降级说明。使用 `--hybrid --rerank evaluate 题集.json` 时，报告使用 hybrid_reranked 标签，并逐题保存真实 applied/fallback 状态与来源版本；不能仅凭启用了配置就宣称精排已运行。

[18 项新场景](../tests/features/test_document_reranking.py) 覆盖候选池、原排名/原文、部分失败整体回退、非法后端输出、明确前缀、输出预算、取消、关闭/空池、本地加载与显式准备、缺少依赖、工具日志/离线重放、配置及评估降级标签。相关共 146 项通过，Ruff 与 Pyright 通过；提交门禁执行全量 pytest。Cross-encoder 分数使用受控后端验证工程流程，B09.2 当时未运行真实精排模型；后续 B09.3 已保存独立真实对照，不能用这里的受控测试代替其质量证据。
