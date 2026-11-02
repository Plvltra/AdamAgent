"""隔离建立后执行的可信入口；只允许选中的只读 ELF 和原生动态加载器再次执行。

本文件作为固定 bootstrap 文本运行，兼容发行版 Python 3.8。Landlock 不替代
Bubblewrap 和 seccomp；匿名可执行文件入口由外层 memfd_create 拒绝规则封闭。
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import shutil
import sys
from pathlib import Path


class Ruleset(ctypes.Structure):
    """只处理 EXECUTE；文件写入和网络由外层固定策略负责。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    """Linux UAPI 的 packed 路径规则；FD 只在添加规则期间保持打开。"""

    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def native_executable(name: str) -> str:
    """返回可信只读运行时内的真实 ELF 路径；不接受脚本、PE 或 /work 程序。"""
    found = name if name.startswith("/") else shutil.which(name, path="/usr/bin:/bin")
    if not found:
        raise RuntimeError("native_program_missing")
    path = Path(found).resolve(strict=True)
    if not any(str(path).startswith(root + "/") for root in ("/usr", "/bin", "/lib", "/lib64")):
        raise RuntimeError("native_runtime_required")
    with path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise RuntimeError("native_elf_required")
    return str(path)


def restrict_execution(program: str) -> None:
    """在当前线程施加不可撤回的执行白名单；缺少内核支持时拒绝启动用户命令。

    首版只支持 x86_64。旧 libseccomp 没有 Landlock 名称，编号取自 Linux
    arch/x86/entry/syscalls/syscall_64.tbl，不为其他体系结构猜测编号。
    """
    if sys.platform == "linux":
        if os.uname().machine != "x86_64":
            raise RuntimeError("unsupported_architecture")
        open_flags = os.O_PATH | os.O_CLOEXEC
    else:
        raise RuntimeError("linux_required")
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    if library.syscall(444, 0, 0, 1) < 1:
        raise RuntimeError("landlock_unavailable")
    rules = Ruleset(1)
    rules_fd = library.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if rules_fd < 0:
        raise RuntimeError("landlock_create_failed")
    try:
        # 文件规则不授权整个 /usr；WSL binfmt 即使持有 /init，也不能执行未授权的 PE。
        for name in {program, native_executable("/lib64/ld-linux-x86-64.so.2")}:
            descriptor = os.open(name, open_flags)
            try:
                rule = PathRule(1, descriptor)
                if library.syscall(445, rules_fd, 1, ctypes.byref(rule), 0):
                    raise RuntimeError("landlock_rule_failed")
            finally:
                os.close(descriptor)
        if library.prctl(38, 1, 0, 0, 0) or library.syscall(446, rules_fd, 0):
            raise RuntimeError("landlock_enforce_failed")
    finally:
        os.close(rules_fd)


def main() -> None:
    """降硬限额、加载执行白名单，再用参数数组替换当前可信解释器。"""
    argv = json.loads(sys.argv[1])
    if sys.platform == "linux":
        for kind, value in (
            (resource.RLIMIT_AS, 268435456),
            (resource.RLIMIT_CPU, 2),
            (resource.RLIMIT_FSIZE, 0),
            (resource.RLIMIT_NOFILE, 32),
            (resource.RLIMIT_CORE, 0),
        ):
            resource.setrlimit(kind, (value, value))
    else:
        raise RuntimeError("linux_required")
    program = native_executable(argv[0])
    restrict_execution(program)
    os.execv(program, [program, *argv[1:]])


if __name__ == "__main__":
    main()
