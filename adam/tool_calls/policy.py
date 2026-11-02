"""工具的读写权限与资源限制；不依赖会话和模型配置。"""

import os
from pathlib import Path

from adam.config.settings import PermissionConfig
from adam.config.settings import ToolLimits as ToolLimits
from adam.contracts.errors import ToolError


class PathPolicy:
    """同时检查请求路径和解析后的真实路径，阻止越界和凭据读取。"""

    def __init__(
        self,
        workspace: Path,
        permissions: PermissionConfig,
        *,
        protected_dirs: tuple[Path, ...] = (),
    ) -> None:
        try:
            self.protected_dirs = tuple(path.resolve() for path in protected_dirs)
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

    def resolve_create(self, value: str) -> Path:
        """授权一个不存在的目标，父目录必须已存在；不创建目录或占位文件。

        复用写入根、凭据、保留目录及链接限制。返回值只是当前观察，发布前仍须复核，
        并使用不覆盖已有目标的原子操作，不能把这次exists检查当成锁。
        """
        path = self._resolve(value, self.write_roots, must_exist=False, writing=True)
        if os.name == "nt" and (
            path.is_reserved()
            or any(":" in part or part.endswith((".", " ")) for part in path.parts[1:])
        ):
            raise ToolError("不允许创建Windows设备名、替代数据流或带尾部点/空格的路径。")
        # lexists也能发现断开的符号链接；任何已有目录项都不是可创建目标。
        if os.path.lexists(path):
            raise ToolError("创建目标已经存在，请改用已有文件的编辑工具。")
        if not path.parent.is_dir():
            raise ToolError("创建目标的父目录必须已经存在；未自动创建目录。")
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
            if any(path.is_relative_to(root) for root in self.protected_dirs):
                raise ToolError("程序保留目录禁止直接访问；子 Agent 只接收主轮次选中的记忆。")
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
