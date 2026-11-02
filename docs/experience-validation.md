# B36 跨轨迹验证经验

B36.1：scripts/experience_evidence.py提供create/link/load/inspect。每条经验记录lesson、applies_when、exceptions和环境版本；link关联显式指定JSON轨迹的SHA256、任务ID、支持/反驳立场及理由。立场是宿主标注，不代表已证明因果，也不以支持票数自动判真。重复任务或同字节轨迹不重复计入。

版本使用独占创建的0000.json等文件，只追加，不覆盖；适用于本地单写者，不做并发合并。来源保持外部引用，inspect检查字节变化/缺失和环境匹配。新增证据令验证状态回到unverified；accepted仍为false，不自动写入长期记忆。最新版本损坏报错，不悄悄回退。

定向1 passed（0.55秒），真实三条教学轨迹关联、正反并存、旧版本保持及来源/环境变化；Ruff/Pyright通过。新任务验证及E4确认桥接留B36.2/3。

B36.2：experience_validation.validate接收宿主TaskProvider和显式execute回调，以新建工作区实际终态验证经验。只允许holdout，拒绝提炼任务ID或保留来源哈希已参与提炼的情况，来源/环境变化先拒绝。保留每次validation.json和哈希、来源、环境、时间，并追加最近状态：supported_on_holdout/counterexample/unverified；执行器/验证器异常不冒充反例。来源标注及回调由可信宿主负责，不能由模型替换。

通过仅代表当前任务结果支持，不证明经验导致改善；无付费请求、无自动记忆接受。定向1 passed（0.46秒），覆盖实际文件新任务、错误回答、执行故障和来源泄漏拒绝；Ruff/Pyright通过。回归对照及E4边界留B36.3。

B36.3：validate的apply_lesson=False运行无经验基线，生成独立报告但不更新经验账本；默认带建议任务可更新最近验证。后续错误终态推翻先前支持状态，环境变化拒绝运行。candidate_text仅生成供E4人工查看/改写的正文，检查当前来源/报告哈希和环境，变化时展示stale；不调用接受接口。用户接受仍走现有/memory accept候选流程，保存不等于验证成功，E4来源verification仍为unverified。

章末4 passed（2.47秒），含实际文件基线/建议对照、后续反例、环境变化和真实E4候选接受落盘；Ruff/Pyright通过。工程范围完成。对照执行器为显式本地回调，未宣称模型因经验变好，也不自动接受记忆。
