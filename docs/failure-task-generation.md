# B35 从失败生成任务

B35.1：scripts/failure_tasks.py 的 generate(root) 在全新目录实际构造三条失败：漏改减法、误改正确差值实现、来源回答错误。每条保存输入、动作、实际文件和独立失败判据，来源明确为constructed_local_failure；不读取用户历史，不冒充真实模型失败，也不训练模型。固定教学内容不含个人信息或密钥。

每类生成两个变体（文件名及查询内容变化），Blueprint保留来源SHA256、输入、任务要求和宿主真值；prepare只写输入文件，verify对实际文件终态/查询来源判定，额外产物失败。当前是严格教学题验证器，不接受任意等价代码，不宣称通用正确性。

定向1 passed（31.77秒），覆盖3条来源和6个变体的失败/成功/额外文件路径；Ruff/Pyright通过。本次验证耗时偏高，未扩展检查。按任务族划分、B31接入及泄漏验收留B35.2/3。

B35.2：failure_suite.load_provider(dataset_path, split)校验源轨迹SHA256和已知蓝图生成结果。operator_repair/preserve_correct整族属于derive，source_lookup整族属于holdout；同族变体不跨集。B31 run_suite新增可选task_provider，缺省仍用原固定题集；每次prepare建立全新独立workspace/session/memory目录，原目录存在即拒绝。manifest保存split、任务族分配、数据集及来源哈希；真值/验证器不写workspace。

定向2 passed（3.77秒），包括两题两次实际应用/模拟HTTP、整族隔离、真实文件重置、来源链及默认入口兼容；Ruff/Pyright通过。仅验证本地工程，不把模拟回答视为真实模型能力。B35.3泄漏/无效变体验收待完成。
