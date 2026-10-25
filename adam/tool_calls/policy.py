"""工具的读取权限与资源限制；不依赖会话和模型配置。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PermissionConfig:
    """工作目录之外，由用户额外授权的读取目录。"""

    additional_read_dirs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ToolLimits:
    """单次读取/结果限制；扫描为协作超时，Git 为每个子进程超时。"""

    max_read_lines: int = 200
    max_result_chars: int = 8000
    max_scan_entries: int = 5000
    max_text_file_bytes: int = 1048576
    max_git_path_list_bytes: int = 1048576
    scan_timeout_seconds: int = 5
    git_timeout_seconds: int = 5


class ToolError(ValueError):
    """可以安全回传给模型的工具参数或权限错误。"""


class PathPolicy:
    """同时检查请求路径和解析后的真实路径，阻止越界和凭据读取。"""

    def __init__(self, workspace: Path, permissions: PermissionConfig) -> None:
        self.workspace = workspace.resolve()
        self.roots = (self.workspace, *(p.resolve() for p in permissions.additional_read_dirs))
        if not self.workspace.is_dir():
            raise ToolError("工作目录不存在或不是目录。")

    def resolve(self, value: str, *, must_exist: bool = True) -> Path:
        """返回已授权的绝对路径；must_exist=False 允许检查 Git 中已删除的路径。"""
        if not value or "\0" in value:
            raise ToolError("路径无效。")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        for path in (candidate.absolute(), resolved):
            if not any(path.is_relative_to(root) for root in self.roots):
                raise ToolError("路径未授权，请由用户配置 permissions.additional_read_dirs。")
            if any(part.lower() in {".ssh", ".aws"} for part in path.parts):
                raise ToolError("凭据目录禁止读取。")
            name = path.name.lower()
            if (
                name == "llm.toml"
                or name.startswith(".env")
                or name in {"id_rsa", "id_ed25519"}
                or path.suffix.lower() in {".pem", ".key"}
            ):
                raise ToolError("凭据文件禁止读取。")
        if must_exist and not resolved.exists():
            raise ToolError("路径不存在。")
        return resolved
