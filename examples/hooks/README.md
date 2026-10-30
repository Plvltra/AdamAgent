# 本地 Hook 示例

两个脚本都使用 Python 标准库，输入来自 stdin 的 JSON，stdout 只输出协议，诊断写 stderr。它们由用户配置启用，默认关闭。

| 示例 | 行为 | 检查范围 |
| --- | --- | --- |
| before_protect_directory.py | 拒绝 replace_text 修改指定目录，模板保护 vendor 和 generated | 专用编辑工具；run_command 和其他工具仍按原有权限及审批处理 |
| after_python_syntax.py | 成功编辑工作目录内的 .py 后，核对实际文件版本并用 ast.parse 检查语法 | 最多1 MiB；不导入或执行目标代码，不证明运行结果正确 |

前置通过仍要经过原有工具权限和人工批准。后置检查发现语法错误时，修改已经发生：退出码非零会产生提示与失败审计，原编辑结果保持 applied，不自动撤销或重做。

## 配置和运行

1. 在主 config.toml 中合入本目录 config.toml 的 hooks 表，替换已有同名表，避免 TOML 重复定义。
2. 模板路径假设工具工作目录就是 Adam 仓库根目录；确认后把 enabled 改为 true。
3. 在其他项目运行时，把两个 argv 的解释器和脚本路径改为绝对路径。检查器的 cwd 仍是本次工具工作目录；vendor/generated 也相对于这个目录。
4. 启动 Adam，在练习目录里准备已有 Python 文件，再要求读取并修改；专用工具不负责创建新文件。

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
