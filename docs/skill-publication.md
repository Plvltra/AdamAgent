# B37 经验到Skill发布

B37.1：scripts/skill_proposal.py 的 propose生成单个skills/<name>/SKILL.md候选、change.diff和proposal.json。目标文件只读，已有正文全部保留并追加经验段；新Skill用name/description标准元数据。提案记录原/新字节哈希、经验账本版本/哈希、假设、适用条件/例外、验证状态和来源。候选输出不能放进已安装skills目录，不自动安装或接受记忆。

首版只生成instruction_only指导型Skill，不生成可执行程序；因此没有新的脚本参数/执行权限。可执行流程若以后加入，必须另行包含参数、前后置条件和失败回退。现有权限、评估器和内核不属于候选目标。

定向1 passed（0.50秒），真实SkillCatalog解析候选、新建/更新原目标不变及版本依据保留；Ruff/Pyright通过。参考skill-creator的范围与简洁元数据要求，采用Adam原生校验器。双集评估、人工采用及回滚留B37.2/3。
