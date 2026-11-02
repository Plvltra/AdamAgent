# B36 跨轨迹验证经验

B36.1：scripts/experience_evidence.py提供create/link/load/inspect。每条经验记录lesson、applies_when、exceptions和环境版本；link关联显式指定JSON轨迹的SHA256、任务ID、支持/反驳立场及理由。立场是宿主标注，不代表已证明因果，也不以支持票数自动判真。重复任务或同字节轨迹不重复计入。

版本使用独占创建的0000.json等文件，只追加，不覆盖；适用于本地单写者，不做并发合并。来源保持外部引用，inspect检查字节变化/缺失和环境匹配。新增证据令验证状态回到unverified；accepted仍为false，不自动写入长期记忆。最新版本损坏报错，不悄悄回退。

定向1 passed（0.55秒），真实三条教学轨迹关联、正反并存、旧版本保持及来源/环境变化；Ruff/Pyright通过。新任务验证及E4确认桥接留B36.2/3。
