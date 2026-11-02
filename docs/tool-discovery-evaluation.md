# B16.3 二十工具候选对照

日期：2026-11-01。固定题目与完整目录见 [manifest](evaluations/b16-tool-discovery/manifest.json)，实际二十四次目录结果及开销见 [report](evaluations/b16-tool-discovery/report.json)。

## 范围和结果

[评估入口](../scripts/tool_discovery_eval.py) 注册二十个用途相近的教学工具，包括文件内容/位置/属性、提交列表/差异、聊天/记忆/资料及编辑/命令。它们只有目录，执行处理函数直接拒绝；并未给 Adam 新增二十项生产工具。先保存目录、参数、能力和十二道固定题，再调用生产 list_tools 与 discover_tools，最后汇总。阈值固定0.5，第一页最多3项，没有看到结果后改题或调阈值。

| 指标 | 原关键词子串匹配 | 本地语义 |
| --- | --- | --- |
| 十道正例 Recall@3 | 1/10 | 9/10 |
| 两道负例返回空候选 | 2/2 | 1/2 |
| 非期望候选数合计 | 0 | 22 |
| 违反当前模式的候选数 | 0 | 0 |
| 后端失败降级次数 | 0 | 0 |
| 十二次目录查询总耗时 | 小于逐次毫秒取整精度 | 1453 ms |
| 首次语义查询，含加载与二十项编码 | 不适用 | 1140 ms |
| 候选结果 UTF-8 字节合计 | 956 | 8007 |

另十一道语义题共313 ms，包含最后切换 plan 模式后重新编码15项允许工具，不是纯缓存查询基准。二十项 ACT 目录编码一次；随后相同模式复用向量；PLAN 目录再编码一次，总共35段和12次查询编码。单次机器运行不能代表稳定性能。

这里的关键词策略是现有名称/分组/摘要子串匹配，不是 BM25；整句自然语言通常不能直接命中。此对照回答“现有目录加入语义排序是否能找到同义用途”，不能证明它优于所有词法检索，也不能直接推算聊天任务成功率。

## 保留的失败与边界

- “在源码中定位配置加载函数出现的位置”没有在前3项找到 search_text，而返回 run_command、git_diff、list_directory。结果有分数不意味着工具适合执行，仍需完整 Schema 和原执行授权。
- “回忆用户以前对这个项目提出的偏好”找到 search_memories，但同时返回 list_memories 和不相关的 delete_file。所有多余候选都保留在报告，不计为成功目标。
- 普通知识题“解释希伯来字母”两种策略均返回空候选。这只测试目录检索，不代表聊天模型一定不会主动调用 list_tools。
- PLAN 模式查询 replace_text 时，语义返回三个只读工具；被禁止的修改/命令既未送入编码器，也未返回候选。权限过滤正确与结果相关是两个指标，不能合并成一个成功率。
- 报告中的 selected 是本次候选页名称；程序显式发现这些名称仅为了测下一请求 Schema 大小，没有发送聊天请求或执行候选。all_allowed_schema 不含发现入口，initial/selected_schema 包含入口，不能直接把大小差解释为整轮 token 节省。模型实际请求次数和费用未测量。

## 环境与复现

实际使用 FastEmbed 0.9.1、CPUExecutionProvider、单线程和 BAAI/bge-small-zh-v1.5，模型缓存快照46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59。复用此前已准备的隔离环境和模型，HF_HUB_OFFLINE=1；未下载、未请求付费聊天 API。运行入口不读用户会话或模型密钥配置。

```powershell
# 在已安装 documents 可选依赖、已准备模型的环境中执行。
$env:HF_HUB_OFFLINE = "1"
python -m scripts.tool_discovery_eval .cache/tool-eval-new --model-cache <实际documents_dir>
```

输出目录必须不存在，避免覆盖以前结果。先有 manifest，再逐题保存，全部完成才写 report；保存失败时保留已完成证据，不生成成功汇总。正常查询禁止自动下载模型。

首次误用了未安装可选依赖的主环境，十二道语义请求全部明确降级；[失败环境报告](evaluations/b16-tool-discovery/missing-backend-report.json) 和 [对应清单](evaluations/b16-tool-discovery/missing-backend-manifest.json) 同时保留。其 backend=local_model 表示请求的运行方式，实际策略必须看每行 strategy/fallback_reason，不能当作已运行模型。

## 工程验收

[7项业务测试](../tests/features/test_tool_discovery_evaluation.py) 使用故意同分的编码器检查误召回如实统计、模式过滤先于编码、Schema/目录版本/完整页、依赖或向量失败、显式缓存不下载、拒绝覆盖及保存中断。相关发现/调用/重放/架构共115项通过（29.54秒）；Ruff、Pyright通过。真实本地模型结果独立归档，测试替身不计为模型效果。

B16.3 完成的是可复现候选对照和权限/开销验收。真实聊天选择工具的效果仍需独立模型任务评估；不会据此自动打开所有会话的语义模式。
