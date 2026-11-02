# B38 独立验收驱动完成

B38.1：contracts.verification定义TaskVerifier与VerificationResult；宿主显式登记/选择VerifierCatalog，模型无注册或修改接口。首个FileStateVerifier冻结1～32个文件的预期哈希（每个最大1 MiB），只读核对实际终态并返回passed/failed/unknown、标准版本及观察哈希。缺失或不符为失败，读取故障为未知，不执行产物代码，不检查未列文件。实际约束由宿主提供，不从模型回答推导。

定向2 passed（0.67秒），覆盖自述成功无效、可变预期字典不影响快照、实际文件修复、缺失/读取故障与未知验收器拒绝；Ruff/Pyright通过。尚未接入聊天收尾，循环留B38.2。
