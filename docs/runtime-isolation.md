# B19.1：固定策略的 WSL 隔离后端

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

这是一种单进程、无文件写入的计算策略，适合 B14 的循环、筛选、聚合。需要文件或命令动作时，B14 必须通过窄 RPC 回到宿主注册工具，继续接受原权限、额度、Hook、批准和日志约束；本阶段没有实现 RPC。多进程库、线程库和普通构建命令不适用此策略，不为它们静默放开权限。

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
