"""工具的读写权限与资源限制；不依赖会话和模型配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

from adam.errors import AgentError


@dataclass(frozen=True)
class PermissionConfig:
    """工作目录之外的读取与写入授权分别配置，互不隐含授权。"""

    additional_read_dirs: tuple[Path, ...] = ()
    additional_write_dirs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ToolLimits:
    """单次读取/结果限制；扫描为协作超时，Git 和通用命令为子进程超时。"""

    max_read_lines: int = 200
    max_result_chars: int = 8000
    max_scan_entries: int = 5000
    max_text_file_bytes: int = 1048576
    max_git_path_list_bytes: int = 1048576
    scan_timeout_seconds: int = 5
    git_timeout_seconds: int = 5
    command_timeout_seconds: int = 30


class ToolError(AgentError):
    """可以安全回传给模型的工具参数或权限错误。"""


class PathPolicy:
    """同时检查请求路径和解析后的真实路径，阻止越界和凭据读取。"""

    def __init__(self, workspace: Path, permissions: PermissionConfig) -> None:
        try:
            self.workspace = workspace.resolve()
            self.roots = (self.workspace, *(p.resolve() for p in permissions.additional_read_dirs))
            self.write_roots = (
                self.workspace,
                *(p.resolve() for p in permissions.additional_write_dirs),
            )
            if not self.workspace.is_dir():
                raise ToolError("工作目录不存在或不是目录。")
        except OSError as error:
            raise ToolError("工具工作目录或额外读取目录无法访问，请检查路径和权限。") from error

    def resolve(self, value: str, *, must_exist: bool = True) -> Path:
        """返回已授权的绝对路径；must_exist=False 允许检查 Git 中已删除的路径。"""
        return self._resolve(value, self.roots, must_exist=must_exist)

    def resolve_write(self, value: str) -> Path:
        """检查独立写入范围及凭据保护；拒绝 .git、链接路径和非普通文件。"""
        path = self._resolve(value, self.write_roots, must_exist=True, writing=True)
        if not path.is_file():
            raise ToolError("只能修改已有的普通文件。")
        return path

    def _resolve(
        self, value: str, roots: tuple[Path, ...], *, must_exist: bool, writing: bool = False
    ) -> Path:
        """读写共用词法与真实路径校验；写入额外拒绝通过链接替换目标。"""
        if not value or "\0" in value:
            raise ToolError("路径无效。")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if writing and Path(os.path.abspath(candidate)) != resolved:
            raise ToolError("暂不通过符号链接或目录联接写入文件。")
        for path in (candidate.absolute(), resolved):
            if not any(path.is_relative_to(root) for root in roots):
                setting = "additional_write_dirs" if writing else "additional_read_dirs"
                raise ToolError(f"路径未授权，请由用户配置 permissions.{setting}。")
            if writing and any(part.lower() == ".git" for part in path.parts):
                raise ToolError("禁止通过编辑工具修改 .git。")
            if any(part.lower() in {".ssh", ".aws"} for part in path.parts):
                raise ToolError("凭据目录禁止访问。")
            name = path.name.lower()
            if (
                name == "llm.toml"
                or name.startswith(".env")
                or name in {"id_rsa", "id_ed25519"}
                or path.suffix.lower() in {".pem", ".key"}
            ):
                raise ToolError("凭据文件禁止访问。")
        if must_exist and not resolved.exists():
            raise ToolError("路径不存在。")
        return resolved
