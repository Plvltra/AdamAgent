# E4 反思与经验对照验收

工程验收使用模拟 HTTP；文件、审批入口、编辑、子进程和 JSONL 实际运行。真实模型质量尚未验收，不把预设响应的成功率当成模型能力。

## 已完成的离线对照

[test_reflection_acceptance.py](../tests/features/test_reflection_acceptance.py) 在相同初始文件上比较：

| 场景 | 关闭专门反思 | 开启反思并接受经验 |
| --- | --- | --- |
| 首次加法缺陷 | 普通反馈循环修正、独立检查通过 | 专门反思后修正、独立检查通过，额外一次请求 |
| 新文件累计加法缺陷 | 先观察检查失败再修正 | 假模型读取相关经验后直接检查代码、修正并验证 |
| 差值反例 | 保持减法，检查通过 | 即使选入加法经验，也保持减法，检查通过 |

假模型的策略是预设的，用来证明经验能传到新任务、改变工具路径且不替代验证。它不能证明真实模型一定会采用、泛化正确或节约费用。
本组首次任务多一次模型请求；迁移任务少一次失败的命令调用，但模型请求次数相同。反思不会天然省 token，是否值得加入要看真实新任务收益。E4.5 另覆盖无关问题不召回、删除/过期和子继承。

## 手动真实模型入口

在 Adam 仓库根目录运行，评估目录必须不存在：

~~~powershell
$evaluationRoot = Join-Path $env:TEMP "Adam-E4-evaluation"
.\.venv\Scripts\python.exe -m scripts.reflection_eval prepare $evaluationRoot
~~~

脚本生成 baseline 和 reflection 两组，每组有独立工作目录、配置、会话和记忆；不会复制密钥、发起模型请求或自动执行代码。
两个工作目录初始完全相同，唯一预设差别是 reflection.enabled。

用户需要亲自启动各组，使用同一模型配置。例如：

~~~powershell
.\.venv\Scripts\python.exe -m adam --config "$evaluationRoot\baseline\config.toml" --workspace "$evaluationRoot\baseline\workspace" --llm-config "$env:USERPROFILE\.adam\llm.toml"
.\.venv\Scripts\python.exe -m adam --config "$evaluationRoot\reflection\config.toml" --workspace "$evaluationRoot\reflection\workspace" --llm-config "$env:USERPROFILE\.adam\llm.toml"
~~~

逐组完成 tasks.json 的四个问题，模型请求会产生费用。命令和编辑沿用正常逐次批准，不批量预先批准。

1. train：复现并修复 train.py，要求运行 Python -B check.py train。检查脚本不允许修改。反思组完成后先看 /memory candidates，再查看原始依据，仅接受有适用范围的候选。
2. /new 后做 transfer：换成 transfer.py 的累计加法，运行 Python -B check.py transfer。两组使用相同新任务提示，不额外告诉其中一组答案。
3. /new 后做 counterexample：difference.py 的差值必须保持减法，用 Python -B check.py counterexample 验证，观察是否错误套用加法经验。
4. /new 后问无关问题：解释幂等，不需要本地工具；检查是否发生无关召回或无意义反思。
5. 两组退出，运行只读汇总：

~~~powershell
.\.venv\Scripts\python.exe -m scripts.reflection_eval report $evaluationRoot
~~~

汇总列出每轮真实请求数、反思请求、工具数、命令退出码、服务端用量、未知用量、耗时及选入记忆 ID；同时核对 check.py 是否原样保留。
它不执行生成代码、不替你认定任务通过，也不根据模型自述计算成功率。需要人工核对检查脚本完整、执行命令和结果，以及原缺陷确实修复。
API 金额随模型、缓存和价格变化；先比较服务端 token、请求和耗时，再按当时账单判断成本。

## 真实效果记录（待用户运行）

每组建议至少三次全新目录重复，保持模型、任务和批准策略一致。记录：

- 初始任务是否修复、新任务是否修复、反例是否误改。
- 用户纠正识别是否准确，是否发生无关工具或反思。
- 经验内容是否有明确依据、适用范围与限制；未验证标记是否保留。
- 请求/工具数、可靠 token、耗时，未知消费单独列出。
- 收益不明显或误用增多时，可在 [reflection] 设置 enabled = false，仍保留 V4 普通反馈修正循环。

不为了完成表格虚构结果；本轮没有运行付费模型。
