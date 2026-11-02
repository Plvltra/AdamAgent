# B19：固定策略的 WSL 隔离后端

本阶段完成 B14 的前置：提供 [WslBubblewrap](../adam/isolation/backend.py) 独立 API 和实际能力探测。尚未接入 run_command，也没有改变已有命令的宿主执行方式；命令接入、批准、取消和清理属于 B19.2，组合故障验收属于 B19.3。不能把 B19 整体视为完成。

当前只实现一个后端：Windows → 显式选择的 WSL 2 发行版 → Bubblewrap。已在本机 `Ubuntu` 上验证；另一个 `Ubuntu-24.04` 未安装所需隔离工具，不自动切换、安装依赖或更改系统策略。宿主 Python 仍需 3.11+，发行版内的独立启动器只用标准库，兼容实际安装的 Python 3.8。

## 执行边界

`probe()` 实际启动固定探针；`run_python(code)` 先探测再执行完整 Python 代码，不限制 Python 内置函数。缺少工具、libseccomp、命名空间能力、权限或协议失败都拒绝执行，不退回宿主进程；失败也不自动重试。

| 项目 | 固定策略 |
| --- | --- |
| 文件系统 | 只读绑定发行版 `/usr`、`/bin`、`/lib`、`/lib64`，其内容视为可信运行时；新的只读根目录与空 `/work` |
| 宿主资料 | 不挂载工作目录、用户目录、Windows 盘符、宿主 socket；没有项目文件直接读写权限 |
| 命名空间 | 新建 user、PID、network、IPC、UTS；独立 `/proc`、最小 `/dev`、新终端会话，移除 capabilities |
| 内核过滤 | 禁止派生进程/线程、socket、切换命名空间等操作；解释器启动后再禁止 execve/execveat |
| 资源 | 256 MiB **地址空间**、2 秒 CPU 的硬限额；5 秒墙钟、64 KiB 合计 stdout/stderr，由外部可信启动器看守 |
| 其他限制 | 文件大小硬限额 0、打开 FD 32、core 0；代码输入最多 64 KiB，输出按 UTF-8 替换非法字节 |
| 环境 | 只传 PATH、LANG、HOME；Bubblewrap 按固定工作目录增加 PWD；不继承密钥、WSLENV 或模型配置 |

这是一种单进程、无文件写入的计算策略，适合 B14 的循环、筛选、聚合。[B14.1/2](code-orchestration.md) 已通过窄 RPC 回到宿主原执行器，文件和命令动作继续接受原权限、额度、Hook、批准和日志约束；工具实际动作不在这个计算 worker 内执行。多进程库、线程库和普通构建命令不适用此策略，不为它们静默放开权限。

再次 exec 的限制有实际依据：本机 WSL 的 binfmt 注册以 `PF` 标志保留 `/init` 解释器，隐藏磁盘路径不能独自保证不存在 Windows 互操作。第二道 seccomp 过滤在可信 Python 启动后加载，随后用户代码连 `/bin/true` 也不能替换执行；测试直接检查 execveat 系统调用，兼容缺少对应 C 库包装的旧发行版。

启动器始终位于宿主可信文件中，在建立隔离之前只处理固定协议。Linux 子进程的原始双流不会直接冒充宿主协议；启动器收集、限额后再封装 JSON。异常退出保留退出码和部分输出；墙钟/输出停止有独立原因。即使程序主动关闭双流后继续休眠，也会被墙钟终止。

资源限额不是整个 WSL 虚拟机的内存上限。该后端也不声称抵抗所有内核漏洞；Bubblewrap 参数、运行时内容与内核更新仍是其可信基础。当前不支持自定义 WSL 盘符映射或跨平台矩阵，映射不符会报告不可用。

## 验收入口和证据

```powershell
python -m scripts.isolation_acceptance --distribution Ubuntu --output isolation-result.json
```

输出路径必须不存在。入口不使用模型，不访问网络，不修改宿主项目文件；真实后端不可用会失败，不跳过也不生成“通过”报告。

[宿主协议测试](../tests/features/test_isolation_backend.py) 的 24 项场景覆盖输入、发行版选择、探测失败、返回格式、部分输出、无重试和环境变量过滤；这些替身只验证宿主通信，**不证明 OS 隔离有效**。连同新增包依赖检查共 46 项通过。

[真实报告](evaluations/b19-isolation.json) 独立记录 12 项能力探针与 8 项执行结果：普通计算、实际内存拒绝、CPU 停止、墙钟停止、关闭双流后停止、输出超限、非零退出、环境和宿主路径不可见。实际版本为 Bubblewrap 0.4.0、发行版 Python 3.8.10；仅证明这次固定策略和案例的观察，不推断任意程序都可安全运行。

## 原始依据

- [Bubblewrap 官方说明](https://github.com/containers/bubblewrap/blob/main/README.md)：命名空间、挂载、新会话及由调用者定义策略的边界。
- [Linux seccomp 文档](https://www.kernel.org/doc/html/next/userspace-api/seccomp_filter.html)：内核过滤及继承条件。
- [Linux no_new_privs 文档](https://docs.kernel.org/userspace-api/no_new_privs.html)：标志的持久性及权限约束。
- [WSL 官方配置说明](https://learn.microsoft.com/windows/wsl/wsl-config)：磁盘自动挂载和 Windows 互操作；本任务未修改这些配置。


## B19.2.1 原生命令后端

[IsolatedCommands](../adam/isolation/commands.py) 提供独立的 `probe()` 和 `run(argv)` API。它可以启动发行版中的原生 ELF 程序，例如 echo、ls 和 Python。此阶段尚未接入模型工具、终端批准或项目文件；原 run_command 仍在宿主执行，不能因新增 API 就称其已受沙箱保护。B19.2.2/3 将分别处理授权输入快照和工具接入。

新策略名为 `bubblewrap-native-single-process-v1`。它沿用固定 Bubblewrap 命名空间、只读运行时、空 `/work` 和 seccomp 拒绝项，保留 256 MiB 地址空间、2 秒 CPU、0 字节文件写入、32 个 FD；墙钟允许 1～5 秒，合计输出最多 64 KiB。命令必须是数组，不拼接 shell。可显式调用原生 shell 的内置操作，但 fork/线程仍禁止，普通构建和多进程命令不在此策略能力内。脚本文件不能直接作为入口，可显式用受支持解释器运行参数中的脚本。没有网络，没有项目挂载，不自动安装程序。

与 B14 的区别是：这里允许执行选定程序。可信 [linux_command_guard](../adam/isolation/linux_command_guard.py) 在首次用户命令前施加 Landlock EXECUTE 白名单，只允许该程序解析后的只读 ELF 文件及 x86_64 动态加载器；没有给整个 `/usr` 执行权限。seccomp 额外拒绝 memfd_create，避免匿名文件成为执行来源。缺少 Landlock 或不支持的体系结构直接拒绝；不能只删除 B14 的 exec 禁止规则来实现这一能力。B14 的第二道 exec 过滤及原策略不变。

可信 [linux_command](../adam/isolation/linux_command.py) 看守双流、墙钟及宿主输入连接。命令原始 stdout 只作为数据，不能冒充宿主结果帧；协议由 [command_protocol](../adam/isolation/command_protocol.py) 限长和校验。宿主取消关闭输入，Linux 端终止本次独立进程组；通信损坏或未知结果不自动重跑。输出达到硬限额时立即停止，保留已捕获片段及 output_limit，不能把片段称为程序最终输出。内存限制是地址空间，不是整个 WSL 虚拟机内存上限。

本机 WSL x86_64 的实际 Landlock ABI 为 7；旧 libseccomp 不认识这些系统调用名称，所以可信入口仅在明确确认 x86_64 后使用 Linux UAPI 的 444/445/446 编号。所用 EXECUTE 能力自 ABI 1 提供，不依赖新版本额外权限。此实现没有修改 WSL 全局设置、binfmt 注册或内核参数。

原始依据：[Landlock 内核文档](https://docs.kernel.org/userspace-api/landlock.html)、[x86_64 系统调用表](https://github.com/torvalds/linux/blob/master/arch/x86/entry/syscalls/syscall_64.tbl)。固定运行时内容、内核和 Bubblewrap 仍属于可信基础；这些案例不构成任意内核漏洞的隔离证明。

### 后端验收

```powershell
python -m scripts.isolated_command_acceptance --distribution Ubuntu --output native-command-result.json
```

[真实报告](evaluations/b19-native-commands.json) 保留 11 项实际命令结果：原生成功、空工作目录、非零退出、脚本入口拒绝、宿主路径隐藏、普通/关闭双流后的墙钟停止、CPU、内存、输出上限和环境过滤。能力探针另实际检查写入、网络、fork、其他程序执行和 memfd 拒绝。CPU 案例核对实际退出码及耗时，内存案例核对 MemoryError；不是把任何非零退出都当成资源限额有效。

报告的取消仅证明取消了“探测或命令”操作并返回中断，没有在该例确认用户 worker 已经启动或逐一核对 Linux PID。B19.3 仍须补执行中取消、强杀宿主、真实越界及进程身份清理验收。宿主路径案例因为 `/mnt` 不存在而拒绝，不冒称实际 PE 文件的执行白名单检验；PE/FD 入口组合留在 B19.3。

[宿主业务测试](../tests/features/test_isolated_commands.py) 新增21项，使用真实管道子进程验证成功/非零、原输出不冒充协议、拒绝降级、参数限额、故障、取消及回收；[辅助启动器](../tests/features/support/isolated_command_launcher.py) 仅代替通信端，不能证明 OS 隔离。相关隔离、编排清理和架构共75项通过。Ruff、Pyright 和文档链接需随阶段检查，主线按 AGENTS.md 三级验证规则执行，B19结束后全量回归。所有验收付费模型请求为0。
