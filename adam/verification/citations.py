"""宿主限定检索词与文件范围，独立验证子任务返回的定位证据。"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from adam.contracts.verification import VerificationResult


class CitationVerifier:
    """核对 JSON 中 path、line、content、sha256；匹配仅证明定位，不证明语义结论。

    allowed_files 和 needle 由宿主指定，模型不能扩大范围。行号从 1 开始，
    内容须整行相等，文件版本须一致；读取故障返回 unknown。
    """

    def __init__(self, workspace: Path, allowed_files: tuple[str, ...], needle: str) -> None:
        self._root = workspace.resolve()
        if not needle or not allowed_files:
            raise ValueError("empty citation criterion")
        for name in allowed_files:
            path = Path(name)
            if path.is_absolute() or path.drive or ".." in path.parts or ":" in name:
                raise ValueError("invalid citation path")
        self._allowed = frozenset(allowed_files)
        self._needle = needle
        self._version = hashlib.sha256(
            json.dumps([sorted(self._allowed), needle]).encode()
        ).hexdigest()

    def check(self, reply: str) -> VerificationResult:
        """忽略模型的成功声明；只打开宿主允许的文件，最多读取 1 MiB。"""
        observed: tuple[tuple[str, str | None], ...] = ()
        try:
            data = json.loads(reply)
            if not isinstance(data, dict) or set(data) != {"path", "line", "content", "sha256"}:
                raise ValueError("证据字段不完整")
            name, line = data["path"], data["line"]
            if not isinstance(name, str) or name not in self._allowed:
                raise ValueError("文件不在宿主允许范围")
            path = self._root / name
            if path.resolve() != path.absolute() or not path.is_file():
                raise ValueError("文件缺失或路径发生重定向")
            with path.open("rb") as stream:
                raw = stream.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("文件超出验收大小限制")
            digest = hashlib.sha256(raw).hexdigest()
            observed = ((name, digest),)
            lines = raw.decode("utf-8").splitlines()
            if type(line) is not int or not 1 <= line <= len(lines):
                raise ValueError("行号无效")
            if (
                data["sha256"] != digest
                or data["content"] != lines[line - 1]
                or self._needle not in lines[line - 1]
            ):
                raise ValueError("文件版本、行内容或检索条件不符")
        except (ValueError, TypeError) as error:
            return self._result("failed", str(error), observed)
        except OSError:
            return self._result("unknown", "读取失败，未确认成功", observed)
        return self._result("passed", "", observed)

    def _result(
        self,
        status: Literal["passed", "failed", "unknown"],
        reason: str,
        observed: tuple[tuple[str, str | None], ...],
    ) -> VerificationResult:
        return VerificationResult(
            status, "citation", self._version, (reason,) if reason else (), observed
        )
