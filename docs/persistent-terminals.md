# B30 有生命周期的持久终端

- [x] B30.1：独立ID、cwd/env、持久进程和双向管道API。
- [x] B30.2.1：终端会话归属、来源持久记录及恢复不重发。
- [x] B30.2.2：聊天工具、批准展示、取消和关闭接入。
- [x] B30.3：真实shell连续cd/环境、交互、异常退出及组合验收。

TerminalSessions仅供宿主协调线程调用，每个terminal_id对应独立进程。start先批准完整argv、初始cwd与显式env覆盖；send再批准每次原样UTF-8输入及eof。owner由宿主绑定，跨会话读写拒绝。默认无批准回调拒绝启动。最多2个活终端、32条记录，每次输入最多8192字节，最多一个写入在途。

启动沿用白名单环境，不继承任意API密钥；显式env覆盖须展示后批准。进程内cd和环境修改保留在该进程，不影响另一个终端或宿主。审批记录的cwd/env是启动信息，后续shell自行修改后的实际状态不能由宿主猜测。

管道模式支持持久shell和行交互程序，不提供PTY/ConPTY，不支持依赖TTY的全屏程序。stdin线程写入不会阻塞协调线程，accepted表示接受，written只表示写入管道，不证明程序已消费。复用B26的OutputCapture字节游标、头尾容量和进程树回收；退出码属于终端进程，不代表每条输入命令成功。

stop回收指定终端，close回收全部终端，寿命受原命令timeout_seconds限制。活进程只驻内存；不按旧PID重连，不是防逃逸沙箱。B30.2已接入模型工具与持久日志，见下节。

B30.1验证：两个真实Python行交互进程保持目录/环境隔离，逐次输入拒绝、跨owner拒绝和单独取消通过；与原后台启动共5 passed（3.53秒），定向Ruff/Pyright通过。真实shell组合留B30.3。
## B30.2.1 持久协调

TerminalService在独立会话锁内保存每终端JSON快照，先保存启动意图，再创建同ID进程；每次stdin请求先保存正文、eof和turn/request/tool来源，再申请批准。每终端最多64次输入，输入请求、接受、写入管道和已消费不能混为一谈：written/closed仅表示管道写入/关闭，不表示命令执行成功。

query保存有界输出观察和退出码。close回收本宿主终端后保存最后观察；恢复把遗留starting/running标为interrupted，尚未结算输入标为unknown，只返回旧快照，不按PID重连或终止旧进程，不自动重发。工具的start/input审批与正文展示已于B30.2.2接入。

验证：真实进程输入、来源、EOF完成、恢复只读和拒绝重发，以及启动前保存失败共2 passed（0.49秒）；定向Ruff/Pyright通过。真实强杀验收留B30.3。
## B30.2.2 聊天与批准

主模型获得open_terminal、terminal_input、read_terminal、close_terminal。start/input标记RUN_PROCESS，plan模式禁止，主会话可执行模式仍逐次批准；子Agent不注册这组工具。输入来源沿用工具执行器的turn/request/tool关联。终端内部目录受主/子文件与资料工具保护，宿主已批准的通用命令仍不是沙箱。

CLI通过contracts.TerminalRequest展示terminal_id、启动argv、初始cwd（明确不保证等于shell当前目录），启动时展示env覆盖及寿命，输入时展示JSON转义完整正文及eof。仅y批准，EOF拒绝，Ctrl+C沿原轮次取消路径回收当前会话后台任务和终端。

read_terminal接受双流字节游标；close_terminal回收指定终端。退出应用在释放会话锁前回收全部；恢复只读旧观察，不重连。确认回调未配置的宿主默认拒绝执行。registry新增原生object参数类型，env内部键值仍由TerminalSessions校验。

验收：模拟HTTP调用真实终端及CLI批准，输入接受与拒绝分支、输出续读、关闭和来源持久化通过；连同架构检查24 passed（3.76秒），定向Pyright通过。TerminalRequest从工具层迁至contracts，原有效说明迁移并补充初始cwd限制。
## B30.3 真实shell与强杀验收

Windows PowerShell管道模式实际连续执行Set-Location、环境覆盖和后续查询，第二shell保持原cwd/env；exit 7保留进程退出码并拒绝后续输入，不影响第二shell。B30.1/2的Python行交互与stdin EOF仍是独立场景，不用模拟shell状态代替真实操作。

独立宿主在真实输入已经执行、accepted回执尚未保存时被强杀，另一宿主此前无法取得锁；锁释放后恢复为interrupted，原输入标为unknown、来源保留。恢复查询不变，拒绝重连与重发。测试用释放文件回收遗留测试子进程；这不宣称生产宿主强杀能自动回收全部进程。

新增2项通过（2.33秒）；章末局部回归11 passed（8.95秒），对应a6164fe。未调用付费模型，TTY/全屏交互仍不在本章范围。