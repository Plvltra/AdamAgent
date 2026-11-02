"""核对诊断所指文件的当前版本；复用原路径授权、有界读取和取消信号。"""

from adam.contracts.cancellation import CancellationToken
from adam.contracts.diagnostic_context import Freshness
from adam.contracts.errors import ToolError
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot


class DiagnosticFiles:
    """只返回版本是否匹配，不向上下文泄露另一个文件的正文或新版本。"""

    def __init__(
        self, policy: PathPolicy, max_bytes: int, cancel: CancellationToken | None = None
    ) -> None:
        self.policy, self.max_bytes, self.cancel = policy, max_bytes, cancel

    def __call__(self, path: str, expected: str) -> Freshness:
        """读取不可靠或权限撤销标为 unavailable；取消传播，版本匹配不构成文件锁。"""
        if self.cancel:
            self.cancel.check()
        try:
            target = self.policy.resolve(path)
            snapshot = read_snapshot(target, self.max_bytes, self.cancel)
            if self.policy.resolve(path) != target:
                return "unavailable"
        except (OSError, ToolError):
            return "unavailable"
        return "current" if snapshot.version == expected else "changed"
