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

[IsolatedCommands](../adam/isolation/commands.py) 提供独立的 `probe()` 和 `run(argv)` API。它可以启动发行版中的原生 ELF 程序，例如 echo、ls 和 Python。此阶段尚未接入模型工具、终端批准或项目文件；原 run_command 仍在宿主执行，不能因新增 API 就称其已受沙箱保护。B19.2.2 已完成下节的授权输入快照；B19.2.3 已完成下文的独立工具、批准与日志接入。

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

[宿主业务测试](../tests/features/test_isolated_commands.py) 新增21项，使用真实管道子进程验证成功/非零、原输出不冒充协议、拒绝降级、参数限额、故障、取消及回收；[辅助启动器](../tests/features/support/isolated_command_launcher.py) 仅代替通信端，不能证明 OS 隔离。相关隔离、编排清理和架构共75项通过。Ruff、Pyright 和文档链接需随阶段检查，主线按 AGENTS.md 三级验证规则执行。所有验收付费模型请求为0。


## B19.2.2 授权文件快照

[capture_inputs](../adam/tool_calls/command_inputs.py) 接收显式映射，例如 `src/notes.txt` → 项目内原文件。虚拟名称是 `/work` 下的相对 POSIX 路径，拒绝绝对路径、反斜杠、`.`、`..`、规范化别名、重复和文件/目录前缀冲突。只读普通文件，不递归扫描目录；原 PathPolicy 对额外目录、凭据和程序保留目录的限制保持。

输入最多16个，每个最多1MiB，合计最多2MiB；宿主可以降低单文件限制。返回不可变 [CommandInput](../adam/contracts/command_inputs.py) 和实际字节 SHA256，来源路径保留在宿主 CapturedInputs.sources，不送入隔离协议。捕获过程比较句柄前后和路径元数据，并重新读取核对整批版本；CapturedInputs.verify 可在批准后再次授权并完整核对内容，源改变或撤权就拒绝，不能悄悄把批准的旧快照替换成新文件。元数据检查不等于锁住外部编辑；后端执行的始终是已确认的不可变字节。

宿主向可信 Linux 启动器发送受限 base64 数据和摘要，最大协议帧3MiB。Linux 端独立校验全部名称、摘要和配额后，才在自己的私有临时目录构造普通文件，只读绑定到 `/work`。没有宿主项目路径、符号链接、挂载参数或目录树传入；命令不能写回原文件。进程回收后才清理该次临时目录。B14 不传这个内部临时目录参数，保持原空工作目录和禁止再次执行的策略。

底层 API 为 `IsolatedCommands.run(argv, inputs=captured.files)`。B19.2.3 已接入下文的聊天工具、批准展示和持久日志；调用者不能把自己构造的 CommandInput 当作授权。

[业务验收](../tests/features/test_command_input_snapshots.py) 新增25项：字节/来源版本、权限和撤权、虚拟逃逸、受保护文件、前缀冲突、数量/单文件/总量、读取变化、取消及损坏数据拒绝；相关后端、编排和架构117项通过。实际 [快照验收入口](../scripts/isolated_input_acceptance.py) 和 [报告](evaluations/b19-inputs.json) 验证中文及 Windows CRLF 原字节、只出现选中文件、写入拒绝和宿主不可见、源变化拒绝，以及2MiB二进制边界摘要一致；没有调用模型。

```powershell
python -m scripts.isolated_input_acceptance --distribution Ubuntu --output isolated-input-result.json
```

首轮实际验收揭示了测试夹具把 Windows 换行假定为 LF，已改为比较捕获字节；后端本来就保留 CRLF，没有为测试更改输出。Ruff、545个文件格式、Pyright 和链接均须核对；主线按 AGENTS.md 三级验证规则执行。


## B19.2.3 主 Agent 隔离命令工具

`run_isolated_command` 是独立入口，默认关闭。原 `run_command` 的说明与批准界面明确标为宿主执行；二者不会自动互相回退。下面配置只注册工具，启动应用时不探测 WSL；每次获批准的实际动作才核对后端能力。

```toml
[command_isolation]
enabled = true
distribution = "Ubuntu"
```

模型传 `argv`、可选 `inputs` 和1～5秒 `timeout_seconds`。例如 `argv=["cat","src/note.txt"]`、`inputs={"src/note.txt":"note.txt"}`。虚拟工作目录固定 `/work`，不接受 cwd、挂载、环境变量、启动器或放宽策略的参数。输入最多16个、单个1MiB、合计2MiB，沿用原额外读取授权、凭据和程序数据目录保护。主应用还显式保护项目记忆目录。子 Agent 不注册该入口，需要把命令方案交回主 Agent。

[工具适配器](../adam/tool_calls/isolated_commands.py) 先经原参数/模式/规则/Hook 检查，再捕获授权文件快照。批准界面显示 Linux 环境、完整 argv、期限，以及虚拟名、宿主来源和 SHA256；显示用 JSON 转义。批准后重新授权并比较原文件版本，变化就拒绝本次旧批准，不重新捕获内容继续执行。用户拒绝沿原规则停止该轮，既不启动能力探测也不执行命令。

工具只依赖 [CommandRunner 契约](../adam/contracts/command_execution.py)，[bootstrap](../adam/bootstrap.py) 注入真实 IsolatedCommands；工具层不导入 isolation。共享 argv 校验迁到兼容 Linux Python 3.8 的 command_inputs，原协议导出保留，Linux 端没有反向引入宿主取消和错误模块。

批准及版本检查通过后先保存 command_started，再调用后端。开始事件记录 environment、distribution、输入来源/版本和原请求关联；command_finished 保留真实状态、退出码及 reason。正常结束为 completed，墙钟停止为 timed_out，输出硬限额为 output_limited；completed 或 ok=true 仅代表进程正常结束，非零仍是业务失败线索，不能完成计划步骤。执行报告、计划证据、反思和待审查动作限制均识别两种命令。日志校验拒绝结束事件改写批准时的环境与输入。

取消时后端没有交付可靠完整结果，记录 interrupted、未知退出码和 truncated=true，再抛出原中断。隔离能力/通信故障停止本轮，已写开始但没有可靠结束的执行保持 unknown；开始日志失败不启动，结束日志失败不重试。重启读取原事实，不自动重发；录制重放不重新运行命令或读取原输入。原工具数/token预算、拒绝、Hook 和共享执行器保持。

有界命令预览与原附件机制保留环境、退出码、停止原因。512字符等极小额度下，连续失败反馈可省略全部正文，并明确 preview_omitted_chars；附件预览可省略 saved_chars/format 展示字段，但保留完整版本和读取凭据、complete 及失败指纹。原正文、完整来源仍在附件/命令日志中。隔离后端达到64KiB时硬停止，只能称已捕获片段，不能套用宿主 CommandTools 持续读取后的最终尾部保证。

[33项新业务场景](../tests/features/test_isolated_command_flow.py) 验证按需发现、真实 HTTP 工具循环、批准/变更/保留目录、模式/Hook/预算、保存失败、取消/未知状态、计划证据、重启与零执行重放、配置、子任务限制及512字符失败停止。此组替换 OS 后端，不能据此声称验证了内核隔离；真实证据见下面入口和报告。命令、编排、计划、反思、模式、配置及架构合计230项通过（60.52秒）；Ruff、550个文件格式、Pyright、注释与架构单独核对。主线按 AGENTS.md 三级验证规则执行。

```powershell
python -m scripts.isolated_chat_acceptance --distribution Ubuntu --output isolated-chat-result.json
```

[真实入口](../scripts/isolated_chat_acceptance.py) 使用生产组装和真实 WSL，模拟模型 HTTP；[结果](evaluations/b19-command-chat.json) 记录授权 CRLF/中文原字节读取、真实非零退出、只读目录拒写、用户拒绝，以及日志重启和删除源文件后的离线重放。首次路径断言未考虑 Windows 临时目录规范化，已改为比较解析后的路径；受限执行环境的 WSL E_ACCESSDENIED 也没有绕过，获原授权运行后通过。付费请求为0。实际执行中取消、强杀与 Linux PID 身份清理继续留在 B19.3。
