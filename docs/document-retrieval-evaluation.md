# E2 固定文档检索评估

日期：2026-10-29。原始逐题结果：[JSON 报告](document-retrieval-evaluation.json)。

## 数据及口径

六篇人工编写的 MD/TXT、16 道固定题，其中有答案 14 道、无答案 2 道。相同分块（400 字符上限）、相同 Top 3；语义阈值固定 0.5，没有在观察结果后删除失败题或调阈值。

命中要求同时满足：来源匹配，且返回片段包含题目标注的原文。Hit@3 是有答案题的命中率，不是最终回答正确率。两道无答案题统计是否返回空候选；即使返回候选，LLM 仍可能识别证据不足，不能用这一指标代替回答评估。

| 指标 | 关键词 BM25 | 本地 embedding |
| --- | --- | --- |
| 有答案题 Hit@3 | 12/14（85.7%） | 13/14（92.9%） |
| 无答案题空结果 | 2/2 | 2/2 |
| 16 题合计检索耗时 | 491.82 ms | 2349.07 ms |
| 返回片段正文字符合计 | 929 | 991 |

耗时为一次本机运行，包括每题源文件扫描，语义一侧包含首次模型加载；文档向量缓存已存在。这不是稳定性能基准，机器与缓存状态会影响数字。没有请求聊天 LLM，未测真实 token 或人民币费用。

## 保留的失败案例

- “电脑突然关机后任务会自己再执行吗”：关键词无结果；embedding 找到 sessions.md 的中断、不重跑说明。
- “落盘没成功会把以前的数据弄丢吗”：两者都未找到 locking.txt 的原子保存依据，倾向 transactions.md。embedding 未消除“数据库事务”和“文件索引写入”的歧义。
- 结果没有证明语义检索全面优于关键词；此次多命中一题，耗时和返回字符也增加。

## 复现环境

FastEmbed 0.9.1、CPUExecutionProvider、模型 BAAI/bge-small-zh-v1.5；模型仓库 Qdrant/bge-small-zh-v1.5 的缓存快照为 46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59。实际依赖由 uv.lock 锁定，模型文件不提交 Git。

```powershell
uv sync --locked --extra documents
python -m adam.document_search --cache-dir .cache/e2 prepare-model
python -m adam.document_search --root tests/fixtures/documents --cache-dir .cache/e2 --semantic --output .cache/e2/report.json evaluate tests/fixtures/document_questions.json
```

模型缺失或不可加载时查询失败；不会联网补下载或把失败伪装成空结果。准备模型和加载模型的动作明确分开。

## 回答层的验证范围

自动化检查假 HTTP 收到真实文档片段、引用字段及持久化版本；假模型的固定回答仅验证传递流程。真实 LLM 是否忠于证据仍需按相同问题人工核对，不能因为检索命中就勾选真实回答质量。
