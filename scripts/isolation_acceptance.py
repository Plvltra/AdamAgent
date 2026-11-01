"""显式运行真实 WSL 隔离验收；后端不可用直接失败，绝不以 Mock 或跳过记为通过。"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from adam.isolation.backend import POLICY, WslBubblewrap


def verify(distribution: str) -> dict[str, object]:
    """执行固定、无宿主写入的案例；任何观察不符都会失败，不调用模型 API。"""
    backend = WslBubblewrap(distribution)
    capability = backend.probe()
    if not capability.available:
        raise RuntimeError("真实隔离不可用，验收未通过。")
    cases = {
        "normal": "import sys; print(sum(range(10))); print(sys.version.split()[0])",
        "memory": "try:\n    value = bytearray(1073741824)\nexcept MemoryError:\n    print('memory denied')",
        "cpu": "while True: pass",
        "wall": "import time; time.sleep(20)",
        "closed_output": "import os, time; os.close(1); os.close(2); time.sleep(20)",
        "output": "import os; os.write(1, b'x' * 100000)",
        "nonzero": "raise SystemExit(7)",
        "environment": "import os; print(sorted(os.environ)); print(os.path.exists('/mnt/c'))",
    }
    results = {name: backend.run_python(code) for name, code in cases.items()}
    assert results["normal"].exit_code == 0 and results["normal"].stdout.startswith("45\n")
    assert results["memory"].exit_code == 0 and results["memory"].stdout == "memory denied\n"
    assert results["cpu"].exit_code != 0 and results["cpu"].reason == "exited"
    assert results["wall"].reason == "wall_limit"
    assert results["closed_output"].reason == "wall_limit"
    assert results["output"].reason == "output_limit" and len(results["output"].stdout) == 65536
    assert results["nonzero"].exit_code == 7
    # Bubblewrap 按固定 chdir 增加 PWD；这不是继承宿主环境。
    assert results["environment"].stdout == "['HOME', 'LANG', 'PATH', 'PWD']\nFalse\n"
    return {
        "policy": POLICY,
        "distribution": distribution,
        "checked_at": datetime.now(UTC).isoformat(),
        "real_backend": True,
        "capability": asdict(capability),
        "results": {
            name: {**asdict(value), "stdout": value.stdout if name != "output" else "[65536 bytes]"}
            for name, value in results.items()
        },
    }


def main() -> int:
    """输出文件必须不存在；只在全部真实案例通过后写入报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.distribution)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("真实隔离探针与 8 项执行案例通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
