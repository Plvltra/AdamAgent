"""最小可信文件验收器：启动时冻结宿主真值，收尾时只读实际文件。"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from adam.contracts.verification import TaskVerifier, VerificationResult


class FileStateVerifier:
    """核对显式指定文件的字节终态，不检查未列出的文件，也不执行输出代码。

    expected只能由宿主构建，不提供模型工具来设置它；保存独立不可变快照，
    修改调用方字典或工作区中的测试文件都不能替换判据。每个文件上限1 MiB。
    """

    def __init__(self, workspace: Path, expected: Mapping[str, bytes]) -> None:
        if not expected or len(expected) > 32:
            raise ValueError("verifier requires 1..32 expected files")
        self._root = workspace.resolve()
        entries = []
        for name, content in expected.items():
            path = Path(name)
            if (
                not name
                or path.is_absolute()
                or path.drive
                or ".." in path.parts
                or ":" in name
                or not isinstance(content, bytes)
                or len(content) > 1048576
            ):
                raise ValueError("invalid expected file")
            target = self._root / path
            if target.resolve() != target.absolute():
                raise ValueError("verifier path redirects")
            entries.append((path.as_posix(), hashlib.sha256(content).hexdigest()))
        if len({name for name, _ in entries}) != len(entries):
            raise ValueError("duplicate verifier path")
        self._expected = tuple(sorted(entries))
        self._version = hashlib.sha256(json.dumps(self._expected).encode()).hexdigest()

    def check(self, reply: str) -> VerificationResult:
        """忽略回答中的成功声明；缺失/字节不符为失败，读取故障为未知。"""
        diagnostics, observed = [], []
        failed = unknown = False
        for name, expected in self._expected:
            path = self._root / name
            digest = None
            try:
                if path.resolve() != path.absolute() or path.is_symlink():
                    failed = True
                    diagnostics.append(f"{name}：路径重定向，未验收")
                elif not path.exists():
                    failed = True
                    diagnostics.append(f"{name}：文件缺失")
                elif not path.is_file() or path.stat().st_size > 1048576:
                    failed = True
                    diagnostics.append(f"{name}：不是可验收的小型文件")
                else:
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest != expected:
                        failed = True
                        diagnostics.append(f"{name}：终态不符合宿主预设要求")
            except OSError:
                unknown = True
                diagnostics.append(f"{name}：无法读取，未确认成功")
            observed.append((name, digest))
        return VerificationResult(
            "failed" if failed else "unknown" if unknown else "passed",
            "file_state",
            self._version,
            tuple(diagnostics),
            tuple(observed),
        )


class VerifierCatalog:
    """宿主提供可信实例并显式选择名称；不从工作区加载代码、不让模型注册验收器。"""

    def __init__(self, verifiers: Mapping[str, TaskVerifier]) -> None:
        self._verifiers = dict(verifiers)

    def select(self, name: str) -> TaskVerifier:
        """只返回宿主已经登记的实例；未知名称明确拒绝。"""
        if name not in self._verifiers:
            raise ValueError("unknown trusted verifier")
        return self._verifiers[name]
