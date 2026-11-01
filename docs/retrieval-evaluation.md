# B09.3 多证据检索评估

固定题集比较关键词、向量、RRF 融合及可选精排。评估候选证据的相关性与排序，不调用聊天 API，也不把命中当成最终回答正确。

## 运行方式

```powershell
uv sync --extra documents
python -m adam.document_search --cache-dir .cache/b09-model prepare-model
python -m adam.document_search --cache-dir .cache/b09-model prepare-reranker
python -m adam.document_search --workspace tests/fixtures --root documents --cache-dir .cache/b09-model --hybrid --rerank --limit 3 --output .cache/b09-report.json evaluate tests/fixtures/document_multi_questions.json
```

两个 prepare 命令显式下载公开模型。评估只加载本地模型；缺失的精排模型会明确降级，不能算作成功精排。省略 `--rerank` 比较前三路，省略 `--hybrid` 只运行关键词。精排对照始终保留未精排的 hybrid 基线。

## 题集与指标口径

[12 道固定题](../tests/fixtures/document_multi_questions.json) 使用已有人工练习资料：4 道精确词、3 道同义改写、3 道各需两份证据的题及 2 道无答案题。它们不是 Adam 的实际运行规则。10 道有答案题共标注 13 个相关片段，题目与金标准在真实运行前冻结，没有按成绩改题或调整阈值。

新题的 relevant 数组中，每项包含相对 path、源文件字节 SHA-256 version、分块 start_line/end_line 和逐字 evidence。版本、位置、原文均匹配才命中；不能用两个相同来源把召回数翻倍。旧 path/evidence 单证据题继续可用，保留原来的路径尾部和原文匹配口径。新金标准应引用不同分块；一个命中同时符合多个金标准时明确报错。

| 指标 | 含义与分母 |
| --- | --- |
| Hit@k | 有答案题中，前 k 条至少找到一个相关片段的题目比例 |
| MRR@k | 有答案题的首个正确结果排名倒数的平均；没命中记 0 |
| 宏 Recall@k | 每题找回片段数 / 该题全部相关片段数，再按有答案题平均 |
| 微 Recall@k | 全部有答案题的找回片段数 / 全部金标准片段数 |
| 无答案误召回率 | 无答案题中返回了任何候选的比例；与回答是否拒答是不同指标 |

无答案题不进入 Hit、MRR 或 Recall 分母；没有相应题型时指标为 null，不输出虚假的 100%。逐题保留每个金标准的实际名次、候选原文/行号/版本、各路排名及精排实际状态，并按 kind 分组汇总。超过 k 或重复 chunk ID 的结果拒绝评估。

同一条路线内、以及不同路线之间必须使用同一 corpus generation，变化时停止，不将不同版本的成绩放在同一报告。报告只在全部路线完成后写出；遇到该类失败不会覆盖既有输出。金标准不会自动随资料更新，资料改了应重新审查题集，不能为了测试通过自动重写答案。

## 2026-11-01 的真实本地对照

[完整逐题结果](evaluations/b09-retrieval-real.json) 与 [环境、输入及模型身份](evaluations/b09-retrieval-manifest.json) 来自真实 CPU 推理。Embedding 为 BAAI/bge-small-zh-v1.5，精排为 BAAI/bge-reranker-v2-m3-int8；FastEmbed 0.9.1、ONNX Runtime 1.31.1。manifest 保存 ONNX 文件哈希、题集与资料哈希；结果只统一为 LF 序列化，没有筛除失败题或修改分数。绝对路径与 generation 保留当次临时工作目录身份，不要求不同机器的路径哈希一致。

配置固定为 k=3、向量阈值 0.5、每路候选 20、RRF 常量 60；精排查询前缀 256 字符、片段前缀 512 字符。没有调用付费聊天 API，也没有上传文档。

| 指标 | 关键词 | 向量 | 融合 | 融合后精排 |
| --- | --- | --- | --- | --- |
| Hit@3 | 7/10 | 10/10 | 10/10 | 10/10 |
| MRR@3 | 0.70 | 1.00 | 0.95 | 1.00 |
| 宏 Recall@3 | 0.70 | 0.95 | 1.00 | 1.00 |
| 微 Recall@3 | 10/13 | 12/13 | 13/13 | 13/13 |
| 无答案返回空结果 | 2/2 | 2/2 | 2/2 | 2/2 |
| 十二题查询耗时合计 | 291 ms | 2,040 ms | 1,003 ms | 14,508 ms |

实际差异：

- 关键词漏掉三道同义改写题；精确词和三道双证据题找全。
- 向量在恢复规则题找到“不重跑工具”，但漏掉“完整回答保存成功”。Hit@3 仍为 100%，所以必须另外看多片段 Recall。
- 融合补全了该题的另一份证据，但把“答案还没落盘”的正确依据排到第 2；MRR 降到 0.95。
- 精排把这份依据移到第 1；10 道有答案题实际精排成功，2 道空候选题不加载精排任务，无降级。

这只是六份短资料、12 道题、一次顺序运行。耗时包含首次加载、缓存复用和当前机器负载，不能用于稳态性能比较。该次精排较慢，且未在更大或陌生资料集证明收益，所以默认仍关闭。额外无关候选、最终回答质量、长文及不同措辞的普遍效果没有由本实验验收。

## 工程验收

[16 项业务场景](../tests/features/test_retrieval_metrics.py) 覆盖固定金标准对应真实分块、不同分母/部分召回、资料变更、查询与路线间版本变化、重复/超限结果、非法金标准先行拒绝、四路 CLI 和真实结果留证。原单证据 E2 题集继续回归。受控排名只用于验证指标算法和流程；上述真实模型结果独立归档。

评估模块的原“只度量证据”和旧题格式契约保留，新注释说明多证据、无答案分母及混合版本拒绝。架构仍使用原独立 CLI → DocumentSearch → 本地检索路径，见 [架构文档](architecture.md)。提交前执行 Ruff、Pyright 与全量 pytest。
