"""生成两个独立教学文件，实际对照完整旧文本和首尾编辑；不调用聊天模型。"""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.contracts.models import FileChangePlan
from adam.tool_calls.factory import create_tool_registry

START, END = "# BEGIN obsolete block\n", "# END obsolete block\n"
BLOCK = START + "".join(f"obsolete_{index:03d} = {index}\n" for index in range(298)) + END
BEFORE, AFTER = "# preserved prefix\n", "# preserved suffix\n"
REPLACEMENT = "answer = 42\n"


def evaluate(directory: Path) -> dict[str, Any]:
    """只在不存在的新目录创建固定样本，批准对象限定到样本本身，保存实际结果。

    参数体积按同一路径字符串、同一版本和JSON编码统计；不是token或整轮请求成本。
    已有输出目录拒绝覆盖。任一写入故障向上传播，未全部完成时不写成功报告。
    """
    directory.mkdir(parents=True, exist_ok=False)
    original = (BEFORE + BLOCK + AFTER).encode("utf-8")
    expected = (BEFORE + REPLACEMENT + AFTER).encode("utf-8")
    rows = [
        _run_case(directory, method, original, expected)
        for method in ("replace_text", "replace_span")
    ]
    report = {
        "format": "span_edit_comparison_v1",
        "selected_lines": len(BLOCK.splitlines()),
        "source_lines": len(original.splitlines()),
        "expected_version": sha256(expected).hexdigest(),
        "rows": rows,
        "same_result": rows[0]["output_version"] == rows[1]["output_version"],
        "passed": all(
            row["ok"]
            and row["status"] == "applied"
            and row["matches_expected"]
            and row["approvals"] == 1
            for row in rows
        ),
        "saved_argument_bytes": rows[0]["arguments_utf8_bytes"] - rows[1]["arguments_utf8_bytes"],
        "limits": "固定生成300行旧代码，两路均真实编辑且保留完整diff；无模型请求，不统计token、费用或模型选边界的成功率。读取、diff、快照和其他消息成本不包含在参数字节内。",
    }
    _save(directory / "report.json", report)
    return report


def _run_case(directory: Path, method: str, original: bytes, expected: bytes) -> dict[str, Any]:
    """每路独立构造样本与批准闭包，只执行所选编辑工具并保存实际观察。"""
    workspace = directory / method
    workspace.mkdir()
    target = workspace / "sample.py"
    target.write_bytes(original)
    approvals = []

    def confirm(plan: FileChangePlan) -> bool:
        # 仅批准本入口刚生成的教学样本及预定结果，不把自动批准能力交给外部模型或文件。
        if plan.path != target.resolve() or plan.original != original or plan.candidate != expected:
            raise ValueError("unexpected comparison plan")
        approvals.append(plan)
        return True

    registry = create_tool_registry(workspace, confirm_edit=confirm)
    read = json.loads(registry.execute("read_file", '{"path":"sample.py","max_lines":1}'))
    args = {
        "path": "sample.py",
        "expected_version": read["version"],
        "new_text": REPLACEMENT,
        **(
            {"old_text": BLOCK}
            if method == "replace_text"
            else {"start_text": START, "end_text": END}
        ),
    }
    encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    result = json.loads(registry.execute(method, encoded))
    actual = target.read_bytes()
    row = {
        "tool": method,
        "arguments_chars": len(encoded),
        "arguments_utf8_bytes": len(encoded.encode("utf-8")),
        "input_version": read["version"],
        "output_version": sha256(actual).hexdigest(),
        "status": result.get("status"),
        "ok": result.get("ok") is True,
        "matches_expected": actual == expected,
        "approvals": len(approvals),
        "approved_diff_chars": sum(len(plan.diff) for plan in approvals),
    }
    _save(directory / (method + ".json"), {"arguments": args, "result": row})
    return row


def _save(path: Path, value: dict[str, Any]) -> None:
    """逐路保存固定参数和实际观察；已有证据不覆盖。"""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    """显式创建新的对照目录；失败返回非零，保留已经写入的样本与证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.directory)
    except (OSError, ValueError, KeyError):
        print("首尾编辑对照未完成，请检查新的输出目录和已保存证据。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
