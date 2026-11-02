"""通过真实 WSL 验证授权快照可读、不可改，且没有宿主项目挂载。"""

import argparse
import hashlib
import json
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError
from adam.isolation.commands import IsolatedCommands
from adam.tool_calls.command_inputs import capture_inputs
from adam.tool_calls.policy import PathPolicy


def evaluate(distribution: str) -> dict[str, object]:
    """所有文件仅在本次临时目录，读取后的真实内容摘要与宿主快照相互核对。"""
    backend = IsolatedCommands(distribution)
    cases = []
    with tempfile.TemporaryDirectory(prefix="adam-isolated-input-") as directory:
        root = Path(directory).resolve()
        source = root / "notes.txt"
        source.write_text("snapshot 中文\n", encoding="utf-8")
        (root / "not-selected.txt").write_text("private sentinel", encoding="utf-8")
        policy = PathPolicy(root, PermissionConfig())
        captured = capture_inputs(policy, {"src/notes.txt": "notes.txt"})
        captured.verify(policy)
        result = backend.run(["/bin/cat", "src/notes.txt"], inputs=captured.files)
        # Windows 写入可能产生 CRLF；隔离后端必须保留实际字节，不能按终端换行猜测。
        assert result.exit_code == 0 and result.stdout == captured.files[0].data.decode("utf-8")
        cases.append({"name": "selected_read", **asdict(result)})
        code = (
            "import errno,json,pathlib; "
            "print(json.dumps({'files':sorted(str(p) for p in pathlib.Path('/work').rglob('*')),"
            "'host_visible':pathlib.Path('/mnt').exists()}))\n"
            "try:\n open('/work/src/notes.txt','w')\n"
            "except OSError as e:\n assert e.errno in (errno.EROFS,errno.EACCES); print('write-denied')\n"
            "else:\n raise RuntimeError('write permitted')"
        )
        result = backend.run(["python3", "-I", "-c", code], inputs=captured.files)
        lines = result.stdout.splitlines()
        assert result.exit_code == 0 and lines[-1] == "write-denied"
        assert json.loads(lines[0]) == {
            "files": ["/work/src", "/work/src/notes.txt"],
            "host_visible": False,
        }
        assert source.read_bytes() == captured.files[0].data
        cases.append({"name": "read_only_selected_tree", **asdict(result)})
        source.write_text("changed source", encoding="utf-8")
        try:
            captured.verify(policy)
        except ToolError:
            cases.append({"name": "changed_source_rejected_before_execute", "passed": True})
        else:
            raise AssertionError("changed source accepted")
        block = root / "block.bin"
        block.write_bytes(b"\0\xff" * 524288)
        large = capture_inputs(policy, {"a.bin": "block.bin", "b.bin": "block.bin"})
        large.verify(policy)
        result = backend.run(
            [
                "python3",
                "-I",
                "-c",
                "import hashlib,json; print(json.dumps([hashlib.sha256(open(p,'rb').read()).hexdigest() for p in ['a.bin','b.bin']]))",
            ],
            inputs=large.files,
        )
        expected = hashlib.sha256(block.read_bytes()).hexdigest()
        assert result.exit_code == 0 and json.loads(result.stdout) == [expected, expected]
        cases.append({"name": "two_mib_binary_boundary", **asdict(result)})
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "distribution": distribution,
        "scope": "B19.2.2 actual snapshot data transport and read-only WSL mount; no chat tool yet",
        "cases": cases,
        "paid_model_requests": 0,
    }


def main() -> None:
    """后端缺失或断言失败不写通过报告；只创建新的指定输出文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    report = evaluate(args.distribution)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("实际隔离输入验收通过；报告：", args.output)


if __name__ == "__main__":
    main()
