"""WSL 内的可信启动器；仅用标准库，兼容该发行版的 Python 3.8。

本文件以绝对路径单独运行，不导入 Adam。用户代码只在 Bubblewrap 成功建立
命名空间、只读挂载和 seccomp 后执行；不能把本启动器当成受限 Python 解释器。
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

POLICY = "bubblewrap-single-process-v1"
MAX_CODE_BYTES = 65536
MAX_OUTPUT_BYTES = 65536
WALL_SECONDS = 5
MEMORY_BYTES = 268435456
CPU_SECONDS = 2

# 这些拒绝规则补充命名空间；不是完整系统调用白名单或对内核漏洞的保证。
DENIED = (
    "fork",
    "vfork",
    "clone",
    "clone3",
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "pidfd_getfd",
    "open_by_handle_at",
    "bpf",
    "perf_event_open",
    "keyctl",
    "add_key",
    "request_key",
    "io_uring_setup",
    "socket",
    "socketpair",
)

# 在隔离已建立后降下不可提高的硬限额，再执行完整 Python 代码。
BOOTSTRAP_GUARD = """
import ctypes, errno, resource, sys
for kind, value in ((resource.RLIMIT_AS, 268435456), (resource.RLIMIT_CPU, 2),
                    (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 32),
                    (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(kind, (value, value))
# WSL 的 binfmt 注册可能保留 /init 的 FD；隐藏宿主路径不能代替禁止再次 exec。
# 第一道过滤在启动解释器前生效，第二道在可信解释器启动后禁止 execve/execveat。
library = ctypes.CDLL('libseccomp.so.2')
library.seccomp_init.argtypes = [ctypes.c_uint32]
library.seccomp_init.restype = ctypes.c_void_p
library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
library.seccomp_syscall_resolve_name.restype = ctypes.c_int
library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
library.seccomp_load.argtypes = [ctypes.c_void_p]
library.seccomp_release.argtypes = [ctypes.c_void_p]
context = library.seccomp_init(0x7fff0000)
if not context:
    raise RuntimeError('exec_guard_init_failed')
try:
    for name in (b'execve', b'execveat'):
        number = library.seccomp_syscall_resolve_name(name)
        if number == -1 or library.seccomp_rule_add(context, 0x50000 | errno.EPERM, number, 0):
            raise RuntimeError('exec_guard_rule_failed')
    if library.seccomp_load(context):
        raise RuntimeError('exec_guard_load_failed')
finally:
    library.seccomp_release(context)
"""

BOOTSTRAP = (
    BOOTSTRAP_GUARD
    + """
code = sys.stdin.buffer.read(65537)
if len(code) > 65536:
    raise ValueError('code limit exceeded')
exec(compile(code, '<isolated>', 'exec'), {'__name__': '__main__'})
"""
)

PROBE = """
import errno, json, os, pathlib, resource, socket
checks = {'uid_non_root': os.getuid() != 0, 'host_mount_hidden': not pathlib.Path('/mnt').exists(),
          'home_hidden': not pathlib.Path('/home').exists(),
          'memory_limit': resource.getrlimit(resource.RLIMIT_AS) == (268435456, 268435456),
          'cpu_limit': resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)}
for name, action in [('root_read_only', lambda: open('/work/probe', 'w')),
                     ('network_denied', lambda: socket.socket()),
                     ('exec_denied', lambda: os.execv('/bin/true', ['true'])),
                     ('fork_denied', os.fork)]:
    try:
        value = action()
    except OSError as error:
        checks[name] = error.errno in (errno.EROFS, errno.EPERM, errno.EACCES)
    else:
        if name == 'fork_denied' and value == 0:
            os._exit(99)
        checks[name] = False
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
seccomp = ctypes.CDLL('libseccomp.so.2')
seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
number = seccomp.seccomp_syscall_resolve_name(b'execveat')
# 旧 glibc 未必有 execveat 包装；按当前体系结构解析编号，直接验内核拒绝。
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p,
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
libc.syscall.restype = ctypes.c_long
result = libc.syscall(number, -100, b'/bin/true', None, None, 0)
checks['execveat_denied'] = result == -1 and ctypes.get_errno() == errno.EPERM
status = pathlib.Path('/proc/self/status').read_text()
checks['no_new_privileges'] = 'NoNewPrivs:\\t1' in status
checks['seccomp_filter'] = 'Seccomp:\\t2' in status
print(json.dumps(checks, sort_keys=True))
"""


def seccomp_filter(output_fd: int, *, extra_denied: tuple[str, ...] = ()) -> None:
    """生成 BPF；调用者只能追加拒绝项，缺少必要调用名或导出失败时拒绝启动。"""
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.seccomp_export_bpf.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW；坏体系结构由库拒绝。
    if not context:
        raise RuntimeError("seccomp_init_failed")
    try:
        for name in (*DENIED, *extra_denied):
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number == -1 or library.seccomp_rule_add(
                context, 0x00050000 | errno.EPERM, number, 0
            ):
                raise RuntimeError("seccomp_rule_unavailable")
        if library.seccomp_export_bpf(context, output_fd):
            raise RuntimeError("seccomp_export_failed")
        os.lseek(output_fd, 0, os.SEEK_SET)
    finally:
        library.seccomp_release(context)


def command(filter_fd: int, *, bootstrap: str = BOOTSTRAP) -> list[str]:
    """固定运行时挂载；不绑定工作目录、用户目录、Windows 盘符或宿主 socket。

    bootstrap 只能由可信宿主选择固定程序，不接受用户覆盖隔离启动步骤。
    """
    args = [
        "/usr/bin/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
    ]
    for name in ("/usr", "/bin", "/lib", "/lib64"):
        if not Path(name).exists():
            raise RuntimeError("runtime_mount_missing")
        args.extend(("--ro-bind", name, name))
    args.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--dir",
            "/work",
            "--chdir",
            "/work",
            "--remount-ro",
            "/",
            "--seccomp",
            str(filter_fd),
            "--",
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            bootstrap,
        )
    )
    return args


def execute(code: bytes) -> dict[str, object]:
    """运行一个隔离进程，持续排空双流；墙钟/合计输出超限便杀死并回收它。"""
    started = time.monotonic()
    with tempfile.TemporaryFile() as policy, tempfile.TemporaryFile() as source:
        seccomp_filter(policy.fileno())
        source.write(code)
        source.seek(0)
        process = subprocess.Popen(
            command(policy.fileno()),
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"},
            pass_fds=(policy.fileno(),),
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        chunks: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        reason = "exited"
        total = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                # 用户程序可能先关闭双流再继续运行，仍须看守到退出或墙钟耗尽。
                while selector.get_map() or process.poll() is None:
                    if time.monotonic() - started > WALL_SECONDS:
                        reason = "wall_limit"
                        break
                    for key, _ in selector.select(0.05):
                        block = os.read(key.fd, 8192)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = MAX_OUTPUT_BYTES - total
                        chunks[key.data].extend(block[:remaining])
                        total += len(block[:remaining])
                        if len(block) > remaining:
                            reason = "output_limit"
                            break
                    if reason != "exited":
                        break
            if reason != "exited":
                kill_group(process)
            process.wait(timeout=1)
        finally:
            if process.poll() is None:
                kill_group(process)
                process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()
        return {
            "policy": POLICY,
            "reason": reason,
            "exit_code": process.returncode,
            "stdout": bytes(chunks["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(chunks["stderr"]).decode("utf-8", errors="replace"),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def kill_group(process: subprocess.Popen[bytes]) -> None:
    """终止本启动器创建的独立进程组；退出竞争不影响后续回收。"""
    if os.name == "nt":
        raise RuntimeError("linux_required")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    """只接受 probe/run 两种宿主操作；故障仅返回类别，不回显配置或环境。"""
    try:
        if sys.argv[1:] not in (["probe"], ["run"]):
            raise ValueError("invalid_operation")
        code = (
            PROBE.encode("utf-8")
            if sys.argv[1] == "probe"
            else sys.stdin.buffer.read(MAX_CODE_BYTES + 1)
        )
        if not code or len(code) > MAX_CODE_BYTES:
            raise ValueError("invalid_code_size")
        result = execute(code)
        if sys.argv[1] == "probe":
            checks = json.loads(str(result["stdout"])) if result["exit_code"] == 0 else {}
            result = {
                "policy": POLICY,
                "available": bool(checks) and all(v is True for v in checks.values()),
                "checks": checks,
                "exit_code": result["exit_code"],
                "reason": result["reason"],
            }
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"policy": POLICY, "available": False, "reason": "backend_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
