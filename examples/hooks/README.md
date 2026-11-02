# 本地 Hook 示例

两个脚本都使用 Python 标准库，输入来自 stdin 的 JSON，stdout 只输出协议，诊断写 stderr。它们由用户配置启用，默认关闭。

| 示例 | 行为 | 检查范围 |
| --- | --- | --- |
| before_protect_directory.py | 拒绝 replace_text / apply_proposal / create_file 修改或创建指定目录内的文件，模板保护 vendor 和 generated | 专用编辑工具；run_command 和其他工具仍按原有权限及审批处理 |
| after_python_syntax.py | 成功编辑工作目录内的 .py 后，核对实际文件版本并用 ast.parse 检查语法 | 最多1 MiB；不导入或执行目标代码，不证明运行结果正确 |

前置通过仍要经过原有工具权限和人工批准。后置检查发现语法错误时，修改已经发生：退出码非零会产生提示与失败审计，原编辑结果保持 applied，不自动撤销或重做。

## 配置和运行

1. 在主 config.toml 中合入本目录 config.toml 的 hooks 表，替换已有同名表，避免 TOML 重复定义。
2. 模板路径假设工具工作目录就是 Adam 仓库根目录；确认后把 enabled 改为 true。
3. 在其他项目运行时，把两个 argv 的解释器和脚本路径改为绝对路径。检查器的 cwd 仍是本次工具工作目录；vendor/generated 也相对于这个目录。
4. 启动 Adam，在练习目录要求创建新的 Python 文件，或读取并修改已有文件；新建入口只接受不存在的目标和已存在的父目录。

完整配置、错误和恢复语义见 [Hooks 契约](../../docs/lifecycle-hooks.md)。修改模板不会自动改写用户配置。

## 不调用付费模型的验证

在 Adam 根目录运行：

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/features/test_hook_acceptance.py -q
~~~

该测试使用模拟 HTTP、真实编辑工具和两个实际脚本，覆盖：

- 正常 Python 编辑、语法错误后的实际修改保留，目标代码不被执行。
- vendor/generated 拒绝，vendor-copy 这类相似目录名仍可编辑。
- 人工拒绝、plan 模式限制、项目规则先进入上下文。
- 关闭配置后零检查进程；工具结果保存失败不执行后置检查。
- 检查前文件已被外部修改或变大，不把不同版本当作检查成功。
- 提示输出故障仍保留编辑和 Hook 事实。

成功的 note 保存在 Hook 审计中；失败会简短提示，详细原因在 stderr 字段。现有独立诊断入口 python -m adam.diagnostics 接受完整 JSONL 路径，输出每轮 hooks；读取不会启动检查器或修改日志。

这两个脚本是扩展接口的示例。前置目录检查不解析任意 shell 命令；后置语法检查只读取工作目录内的 Python 文件。用户允许在额外目录编辑时，可按自己的检查范围调整示例。Hook 是用户信任的本地程序，其宿主不是操作系统沙箱。

T11.2 的 apply_proposal 在 Hook 前核对 path 与真实候选完全一致，两个示例按相同目录和结果规则处理它。
提案业务验收见 [test_proposal_application.py](../../tests/features/test_proposal_application.py)，包含前置拒绝、后置失败保留实际修改，以及伪造路径在 Hook 前拒绝。
仅按工具名称编写的个人检查器需要自行将新入口纳入策略；项目升级不会改写用户脚本。


B22.3.1将create_file纳入三个仓库示例，语法错误仍不回滚已经创建的文件；[创建业务测试](../../tests/features/test_file_creation_loop.py)运行真实示例验证目录拒绝与两种语法报告。用户自有检查器仍须显式检查新工具名称。

## B20.1 新的结构化示例

[after_python_diagnostics.py](after_python_diagnostics.py) 使用后置响应v2，将语法问题正常交付为有文件版本和位置的诊断；原 after_python_syntax.py 保留其v1/非零退出教学行为。显式替换用户配置的 after_tool.argv 才会使用新示例，不自动改用户配置。新报告保存在 Hook 日志，并经B20.2的版本观察独立回传模型；字段、范围和测试见 [修改后诊断](../../docs/edit-diagnostics.md)。
