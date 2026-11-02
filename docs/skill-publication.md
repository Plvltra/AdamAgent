# B37 经验到Skill发布

B37.1：scripts/skill_proposal.py 的 propose生成单个skills/<name>/SKILL.md候选、change.diff和proposal.json。目标文件只读，已有正文全部保留并追加经验段；新Skill用name/description标准元数据。提案记录原/新字节哈希、经验账本版本/哈希、假设、适用条件/例外、验证状态和来源。候选输出不能放进已安装skills目录，不自动安装或接受记忆。

首版只生成instruction_only指导型Skill，不生成可执行程序；因此没有新的脚本参数/执行权限。可执行流程若以后加入，必须另行包含参数、前后置条件和失败回退。现有权限、评估器和内核不属于候选目标。

定向1 passed（0.50秒），真实SkillCatalog解析候选、新建/更新原目标不变及版本依据保留；Ruff/Pyright通过。参考skill-creator的范围与简洁元数据要求，采用Adam原生校验器。双集评估、人工采用及回滚留B37.2/3。

B37.2：skill_release.evaluate复用B31，在B35 derive/holdout两集对照baseline/candidate。每次试验独立复制单个Skill，经原生加载器使用；已有Skill作为基线，新Skill以无候选为基线。改善集至少一项由失败转成功，两集无退步/缺失且用量已知才生成eligible。gate关联提案/候选及所有评估产物哈希，保留mock_http/live_model区别。

decide将完整diff和评估交给显式宿主批准回调；拒绝记录rejected，批准才写一个Skill。rollback仅在目标仍匹配候选哈希时恢复旧正文或删除新文件，同样经过批准；历史独占追加，记录拒绝/安装/回滚。模拟评估只证明本地工程，不代表真实模型收益；回调须由可信宿主连接人工界面，不能交给模型。

定向1 passed（7.20秒），真实Skill加载、文件编辑、双集对照及人工拒绝/接受/回滚通过；Ruff/Pyright通过。测试修正模拟模型未发现工具的问题后，使用full目录配置；生产默认按需发现保持。版本冲突、失效与中断边界留B37.3。
