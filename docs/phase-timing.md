# B34 阶段耗时与成本

B34.1：scripts/phase_timing.py 的 PhaseTiming 在B31每个试验内包装实际模型调用、工具入口、编辑审批回调和会话持久化入口，结果保存在单次报告phase_timing。首个非空text回调单独记录；没有观测则duration_ms为null，不用总耗时推算。所有区间相对同一单调时钟，包含失败收尾，最多10000条并记录丢弃数。

区间可能嵌套（工具含审批/存储），不能相加冒充墙钟时间。storage是持久化方法入口含校验/序列化，不是纯磁盘或fsync；model含解析和回调，不是纯网络时间。审批是当前固定任务的自动策略，不代表人工反应速度；仅主试验，不完整覆盖子Agent、后台任务或外部存储。未观测阶段明确列出。生产聊天默认不安装此观测。

定向验收：实际应用+模拟HTTP产生阶段记录、首输出及真实文件操作失败/嵌套区间，2 passed（2.10秒）；Ruff/Pyright通过。B34.2聚合和费用、B34.3对照验收待完成。

B34.2：python -m scripts.phase_report <B31 report.json> [--prices prices.json] 输出JSON。按阶段统计观测数、未知/缺失数、中位数和最近秩P95；不求阶段总和。B31结果新增来自已校验日志的request_observations及模型ID。价格JSON要求source/as_of/model/currency=CNY/input_per_million/output_per_million/cached_input_per_million；价格必须显式提供，不内置时效价格。完整费用须用量已知、缓存命中/未命中均已知且模型匹配；缺项不倒算，已知费用只是可计价请求的小计，非账单。

定向2 passed（2.09秒），包含CLI输出、分位数、缓存计价与缺字段，以及真实应用采集兼容；Ruff/Pyright通过。B34.3局部和重复对照仍待完成。

B34.3：两组同一no-tools任务各重复2次，经真实应用和模拟HTTP将缓存计数写入/读回日志，再按组聚合。已知计数按显式测试价格计费，缺失缓存保持未知；另验证缺阶段、截断标记、工具包含审批/存储的重叠区间不会被加总。B34章末及直接受影响B31入口/报告共8 passed（12.59秒），Ruff通过。工程范围完成，测试不比较真实模型速度或账单。
