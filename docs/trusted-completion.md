# B38 独立验收驱动完成

B38.1：contracts.verification定义TaskVerifier与VerificationResult；宿主显式登记/选择VerifierCatalog，模型无注册或修改接口。首个FileStateVerifier冻结1～32个文件的预期哈希（每个最大1 MiB），只读核对实际终态并返回passed/failed/unknown、标准版本及观察哈希。缺失或不符为失败，读取故障为未知，不执行产物代码，不检查未列文件。实际约束由宿主提供，不从模型回答推导。

定向2 passed（0.67秒），覆盖自述成功无效、可变预期字典不影响快照、实际文件修复、缺失/读取故障与未知验收器拒绝；Ruff/Pyright通过。尚未接入聊天收尾，循环留B38.2。

B38.2：build_application显式接受verifier，只注入主Conversation。每轮CompletionVerifier在最终回答保存前检查终态，先记录verification_finished再决定完成或诊断修正。最多反馈两次修正，继续用原RequestPolicy/ToolBudget及编辑批准；第三次失败以verification_failed结束，unknown立即以verification_unknown停止。未通过回答不作为最终消息加入历史，只反馈宿主诊断；启用时暂存正文，避免提前展示成功。

JSONL验收事件关联turn/request和连续attempt，保存标准版本、结果、诊断及文件哈希；read_report逐轮可查。验收通过不等于回答保存成功，仍须原提交路径完整落盘。定向2 passed（2.76秒），实际工具读取/批准修改后成功及持续失败3次请求停止；Ruff/Pyright通过。测试发现并修复历史final协议和修正计数问题。取消/保存故障等B38.3待完成。

B38.3：真实应用验收正确初稿只请求一次、验收中取消、验收日志写入失败、最终消息写入失败及原请求额度停止。启用验收不提前输出正文；取消/保存失败无额外模型请求且无成功历史。最终消息保存失败时可保留passed验收证据，但turn为failed，不能据此宣布整轮成功。

本章功能与包依赖回归32 passed（5.69秒），Ruff通过；工程范围完成。范围为宿主显式注入可信验收器，尚无自动从用户自然语言生成任意判据，也不调用付费模型。
