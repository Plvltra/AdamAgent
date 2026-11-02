# B31 统一任务评估

- [x] B31.1：固定任务、全新试验环境和独立终态验证。
- [x] B31.2：多配置/重复运行、失败分母、用量费用与显式真实模型入口。
- [x] B31.3：开关消融、正常反例及报告。

scripts/task_eval_assets.py定义六个最小任务：指定加法修复、正确差值保持不改、基础记忆回忆、两个已保存会话查证、主动关联项目检查要求、无需工具的普通问题。记忆种子复用B06 scripts.memory_eval.seed_memories；普通问题复用E4 reflection_eval材料，未另造一套记忆存储。

prepare_task只创建全新根目录，已存在即拒绝。每次拥有独立workspace、真实MemoryStore记录和两个真实SessionStore会话；历史ID随机生成并保留在manifest供复核，任务不携带标准答案。环境重置通过新目录实现，不递归清空旧目录，也不复制真实用户配置或密钥。

verify_task由宿主持有固定真值，不执行模型生成的Python。代码题比较目标文件AST与指定实现、检查其他文件未变；其他题检查精确JSON字段与文件不变，跨会话题同时要求真实来源ID，无工具题还检查实际工具列表为空。检查失败、无法解析或缺少证据均不通过，模型自述不作为成功证据。

这些是约束明确的练习题，不是通用代码正确性证明或任意自然语言评分；主动关联题使用既有“运行单元测试”约定，不能单凭答对证明模型一定使用了记忆。机制来源与消融由后续runner记录。真实模型质量目前未验收。

工程验收：实际错误文件拒绝、修复后接受、额外改动拒绝、重置独立、来源校验和工具反例共2 passed（2.66秒），Ruff/Pyright通过；未调用模型。
## B31.2 多配置运行

使用模块入口 scripts.task_eval：显式--live、一个或多个--config、--repeats，以及可选重复--case。output必须是新目录。每个配置×任务×次数独立创建环境和应用，逐次保存结果并更新report.json；未通过验证、API错误均保留。success_rate分母是完整计划矩阵，attempted与not_run单独列出，不把失败隐藏掉。

入口只沿用模型连接、agent/context/memory/planning/reflection/tool_discovery设置；系统提示固定，存储全部重定向到试验目录，不继承外部目录权限、扩展服务或命令批准。仅自动批准试验workspace内编辑，命令和终端默认拒绝。manifest保存有效设置，不保存密钥。

--max-known-tokens是试验间停止阈值：每项结束后累计检查，单项仍受agent会话限额约束，最后一项可能越过总阈值；不是账单硬上限。用量未知或用户中断后，剩余项目保存not_run，不自动重试。费用仅按显式--price-cny输入/输出每百万token单价估算，未提供单价或总用量未知时总费用保持null，不硬编码厂商价格。

没有--live时在加载配置前拒绝执行。程序化工程验收必须注入MockTransport，错误工厂不会退回联网HTTP；报告标为mock_http，不能宣称真实模型效果。真实运行报告标为live_model，但仍只代表固定题结果。

验收：8个隔离试验保留故意错误答案，准确统计12 token/试验与显式单价；未知用量停止和显式启用约束通过，2 passed（7.31秒），定向Pyright通过。未调用付费API。
## B31.3 单开关对照报告

run入口增加--ablate memory、planning或reflection，只接受一个基础配置，生成disabled/enabled两臂，其余有效设置相同。manifest保存服务端地址哈希、模型/协议及主要请求设置；不保存地址中的潜在凭据明文。实际记忆选择通过生产选择器观察接口记录status、策略、ID与revision，不据此推断模型一定使用了记忆。

只读报告入口为 python -m scripts.task_eval_report <结果目录>，默认disabled为基线，可用--baseline指定。报告从manifest逐条读取试验产物，不相信旧聚合成功数字；缺失文件保持计划分母并标missing、用量未知。按case与iteration配对，分别列improved、regressed、both_passed、both_failed、unavailable；正确代码不改和无需工具的普通问题单独统计。

只有配置差异恰为一个支持的enabled字段才标single_mechanism_toggle；其他差异如实列出，不宣称因果或统计显著性。mock_http报告只算离线工程证据，live_model也只代表固定题结果。这里仍没有真实付费模型质量结论。

验收：四个真实应用/模拟HTTP试验验证记忆开关实际改变注入内容与来源记录，正常反例保持通过；删除产物后报告保留分母并标缺失。1 passed（4.34秒），定向Pyright通过。本章局部5 passed（13.56秒），对应2ddf19c；B29～B31全量在该提交独立快照运行中。