"""真实 WSL 原生命令边界：文件、网络、执行入口、资源硬限额和输出解码完整性。"""

import argparse
import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from adam.config.settings import PermissionConfig
from adam.isolation.commands import IsolatedCommands
from adam.tool_calls.command_inputs import capture_inputs
from adam.tool_calls.policy import PathPolicy

# 代码在固定隔离策略内运行；只探测创建能力，不连接外部地址，也不修改宿主文件。
BOUNDARIES = """
import errno, json, os, pathlib, resource, socket, sys
checks = {}
def denied(name, operation, expected):
    try:
        value = operation()
    except OSError as error:
        checks[name] = {'passed': error.errno in expected, 'errno': error.errno}
    else:
        if name == 'fork' and value == 0:
            os._exit(91)
        if hasattr(value, 'close'):
            value.close()
        checks[name] = {'passed': False, 'errno': None}
denied('host_file', lambda: open(sys.argv[1], 'rb'), {errno.ENOENT, errno.EACCES})
denied('parent_proc_root', lambda: open('/proc/1/root' + sys.argv[1], 'rb'), {errno.ENOENT, errno.EACCES})
denied('unselected_file', lambda: open('/work/private.txt', 'rb'), {errno.ENOENT})
denied('parent_path', lambda: open('../../private.txt', 'rb'), {errno.ENOENT})
denied('write_input', lambda: open('/work/selected.txt', 'w'), {errno.EROFS, errno.EACCES})
denied('make_symlink', lambda: os.symlink('/usr/bin/true', '/work/link'), {errno.EROFS, errno.EACCES})
denied('ipv4_socket', lambda: socket.socket(socket.AF_INET), {errno.EPERM})
denied('ipv6_socket', lambda: socket.socket(socket.AF_INET6), {errno.EPERM})
denied('unix_socket', lambda: socket.socket(socket.AF_UNIX), {errno.EPERM})
denied('fork', os.fork, {errno.EPERM})
denied('other_elf_exec', lambda: os.execv('/usr/bin/false', ['false']), {errno.EACCES})
descriptor = os.open('/usr/bin/false', os.O_RDONLY)
try:
    denied('other_elf_fd_exec', lambda: os.execve(descriptor, ['false'], {}), {errno.EACCES})
finally:
    os.close(descriptor)
denied('pe_exec', lambda: os.execv('/work/cmd.exe', ['cmd.exe', '/c', 'exit', '47']), {errno.EACCES, errno.EPERM})
checks['pe_header'] = {'passed': pathlib.Path('/work/cmd.exe').read_bytes()[:2] == b'MZ'}
checks['pe_input_mode'] = {'passed': True, 'mode': oct(pathlib.Path('/work/cmd.exe').stat().st_mode & 0o777)}
opened = []
try:
    for _ in range(64):
        opened.append(os.open('/dev/null', os.O_RDONLY))
except OSError as error:
    checks['fd_limit'] = {'passed': error.errno == errno.EMFILE, 'errno': error.errno, 'opened': len(opened)}
else:
    checks['fd_limit'] = {'passed': False}
finally:
    for descriptor in opened:
        os.close(descriptor)
try:
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
except (OSError, ValueError):
    checks['raise_cpu_hard_limit'] = {'passed': resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)}
else:
    checks['raise_cpu_hard_limit'] = {'passed': False}
status = pathlib.Path('/proc/self/status').read_text()
checks['privileges_and_seccomp'] = {'passed': 'NoNewPrivs:\\t1' in status and 'Seccomp:\\t2' in status}
print(json.dumps(checks, sort_keys=True))
"""

REEXEC = """
import json, os, sys
code = "import json,resource; print(json.dumps({name:resource.getrlimit(kind) for name,kind in [('cpu',resource.RLIMIT_CPU),('memory',resource.RLIMIT_AS),('files',resource.RLIMIT_NOFILE)]}))"
os.execv(sys.executable, [sys.executable, '-I', '-B', '-c', code])
"""


def evaluate(distribution: str) -> dict[str, Any]:
    """命令与原文件真实运行，仅输出本次固定探针的元数据；任何失败均不冒充通过。"""
    backend = IsolatedCommands(distribution)
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="adam-command-boundary-") as directory:
        root = Path(directory).resolve()
        source = root / "selected.txt"
        source.write_bytes(b"selected only")
        private = root / "private.txt"
        private.write_bytes(b"private sentinel")
        pe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        (root / "cmd.exe").write_bytes(pe.read_bytes())
        policy = PathPolicy(root, PermissionConfig())
        batch = capture_inputs(policy, {"selected.txt": "selected.txt", "cmd.exe": "cmd.exe"})
        result = backend.run(
            ["python3", "-I", "-B", "-c", BOUNDARIES, _linux_path(private)], inputs=batch.files
        )
        assert result.exit_code == 0 and result.reason == "exited", asdict(result)
        observed = json.loads(result.stdout)
        assert len(observed) == 18 and all(item["passed"] for item in observed.values()), observed
        assert (
            source.read_bytes() == b"selected only" and private.read_bytes() == b"private sentinel"
        )
        cases.append(
            {"name": "real_boundaries", "checks": observed, "host_sources_unchanged": True}
        )

        result = backend.run(["python3", "-I", "-B", "-c", REEXEC])
        assert result.exit_code == 0 and result.reason == "exited", asdict(result)
        limits = json.loads(result.stdout)
        assert limits == {"cpu": [2, 2], "memory": [268435456, 268435456], "files": [32, 32]}
        cases.append({"name": "selected_program_reexec_keeps_limits", "actual_limits": limits})

        for name, content, loss in (
            ("utf8", "中文\r\n".encode(), False),
            ("literal_replacement", "�".encode(), False),
            ("invalid_bytes", b"prefix\xffsuffix", True),
            ("partial_character", b"\xe4\xb8", True),
        ):
            source.write_bytes(content)
            data = capture_inputs(policy, {"input": "selected.txt"})
            result = backend.run(["cat", "input"], inputs=data.files)
            assert result.exit_code == 0 and result.reason == "exited", asdict(result)
            assert result.invalid_utf8 is loss and result.captured_bytes == len(content)
            assert result.stdout == content.decode("utf-8", errors="replace")
            cases.append(
                {
                    "name": name,
                    "invalid_utf8": result.invalid_utf8,
                    "captured_bytes": result.captured_bytes,
                }
            )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "distribution": distribution,
        "real_backend": True,
        "paid_model_requests": 0,
        "cases": cases,
        "limits": [
            "PE 输入为真实 cmd.exe 副本，但快照不可写且文件无执行位；该例仅证明组合策略拒绝，不单独证明 Landlock。",
            "其他 ELF 的路径及 FD 执行使用原本可执行的运行时文件，单独核对 EACCES。",
            "同一选定 ELF 允许替换当前进程，不能新增进程，继承的硬限额仍有效。",
            "实际执行中取消、强杀与进程身份回收由 B19.3.2 单独验收。",
        ],
    }


def _linux_path(path: Path) -> str:
    """只转换本次临时本地盘符路径，用于尝试读取应当不可见的宿主哨兵。"""
    value = PureWindowsPath(path)
    if len(value.drive) != 2 or value.drive[1] != ":":
        raise ValueError("验收只支持本地盘符临时目录。")
    return "/mnt/" + value.drive[0].lower() + "/" + "/".join(value.parts[1:])


def main() -> None:
    """排他写报告；不安装后端、不改系统设置，失败时保留异常。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = evaluate(args.distribution)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("真实文件/网络/执行入口、资源及编码边界通过；付费请求 0。")


if __name__ == "__main__":
    main()
