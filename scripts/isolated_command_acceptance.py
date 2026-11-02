"""显式运行原生命令隔离验收；无模型、无项目挂载，后端缺失直接失败。"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Timer
from time import monotonic

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.isolation.commands import IsolatedCommands


def evaluate(distribution: str) -> dict[str, object]:
    """运行固定真实命令，保留实际退出和停止事实；任一条件不符不生成通过报告。"""
    backend = IsolatedCommands(distribution)
    if not backend.probe():
        raise RuntimeError("原生命令隔离不可用，未执行验收。")
    cases = []
    specs = [
        ("native_echo", ["/bin/echo", "native-command-ok"], 5),
        ("empty_workspace", ["/bin/ls", "-A", "/work"], 5),
        ("nonzero", ["/bin/false"], 5),
        ("runtime_script_denied", ["/usr/bin/ldd", "/bin/true"], 5),
        ("host_program_denied", ["/mnt/c/Windows/System32/cmd.exe", "/c", "exit", "42"], 5),
        ("wall_limit", ["python3", "-I", "-c", "import time; time.sleep(30)"], 1),
        (
            "closed_streams",
            ["python3", "-I", "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"],
            1,
        ),
        ("cpu_limit", ["python3", "-I", "-c", "while True: pass"], 5),
        ("memory_limit", ["python3", "-I", "-c", "x=bytearray(512*1024*1024)"], 5),
        ("output_limit", ["python3", "-I", "-c", "import os; os.write(1,b'x'*100000)"], 5),
        (
            "environment",
            ["python3", "-I", "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
            5,
        ),
    ]
    for name, argv, timeout in specs:
        result = backend.run(argv, timeout_seconds=timeout)
        if name == "native_echo":
            passed = result.exit_code == 0 and result.stdout == "native-command-ok\n"
        elif name == "empty_workspace":
            passed = result.exit_code == 0 and not result.stdout
        elif name in {"wall_limit", "closed_streams"}:
            passed = result.reason == "wall_limit" and result.exit_code != 0
        elif name == "output_limit":
            passed = result.reason == "output_limit" and result.captured_bytes == 65536
        elif name == "cpu_limit":
            passed = (
                result.reason == "exited"
                and result.exit_code in {-9, 137}
                and result.duration_ms >= 1500
            )
        elif name == "memory_limit":
            passed = result.exit_code != 0 and "MemoryError" in result.stderr
        elif name == "runtime_script_denied":
            passed = result.exit_code != 0 and "native_elf_required" in result.stderr
        elif name == "host_program_denied":
            passed = result.exit_code != 0 and "FileNotFoundError" in result.stderr
        elif name == "environment":
            passed = result.exit_code == 0 and set(json.loads(result.stdout)) <= {
                "PATH",
                "HOME",
                "LANG",
                "PWD",
                "LC_CTYPE",
            }
        else:
            passed = result.reason == "exited" and result.exit_code != 0
        if not passed:
            raise RuntimeError(f"隔离验收失败：{name}，{result.reason}/{result.exit_code}")
        data = asdict(result)
        if name == "output_limit":
            data["stdout"] = "<omitted from report>"
        cases.append({"name": name, "passed": True, **data})
    token = CancellationToken()
    timer = Timer(1, token.cancel)
    started = monotonic()
    timer.start()
    interrupted = False
    try:
        backend.run(["python3", "-I", "-c", "import time; time.sleep(30)"], cancel=token)
    except RequestInterrupted:
        interrupted = True
    finally:
        timer.cancel()
        timer.join()
    if not interrupted or monotonic() - started >= 8:
        raise RuntimeError("取消验收失败")
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "B19.2.1 real WSL native command backend; no project input or chat integration",
        "distribution": distribution,
        "capability_probe": True,
        "cases": cases,
        "cancellation": {
            "scope": "cancels capability-or-command operation; no active-worker identity assertion",
            "interrupted": True,
            "duration_ms": round((monotonic() - started) * 1000),
        },
        "paid_model_requests": 0,
        "limitations": [
            "single process",
            "read-only empty workspace",
            "no network",
            "x86_64 WSL only",
        ],
    }


def main() -> None:
    """报告独占创建；失败不覆盖既有证据，也不调用付费模型。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = evaluate(args.distribution)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("真实命令隔离验收通过；报告：", args.output)


if __name__ == "__main__":
    main()
