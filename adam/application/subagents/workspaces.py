"""宿主拥有的独立 Git 副本；副本不共享索引、对象写入或主项目工作文件。"""

import json
import os
import secrets
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError


@dataclass(frozen=True)
class PrivateCopy:
    """工作文件使用 path；规则与记忆仍属于原项目，外部写授权不传给子副本。"""

    copy_id: str
    path: Path
    source: Path
    base_commit: str
    permissions: PermissionConfig

    @property
    def rules_root(self) -> Path:
        return self.source

    @property
    def memory_identity(self) -> Path:
        return self.source


class PrivateWorkspaces:
    """在主会话锁保护下管理副本；只接受干净的普通本地仓库。

    root 必须在原项目之外。清理只删除自己登记的副本，保留生命周期记录；
    调用者必须先等待子任务退出，才能 cleanup 或 recover_abandoned。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, source: Path, permissions: PermissionConfig) -> PrivateCopy:
        """克隆固定 HEAD 到独立仓库，失败自动清理；不复制未提交或忽略文件。"""
        source = source.resolve()
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise ToolError("私有副本根目录必须与原项目分离。")
        if not (source / ".git").is_dir() or (source / ".git").is_symlink():
            raise ToolError("私有副本目前只支持普通 Git 仓库根目录。")
        if Path(self.git(source, ["rev-parse", "--show-toplevel"]).strip()).resolve() != source:
            raise ToolError("必须使用原仓库根目录。")
        if any(
            line.lower().startswith("filter.")
            for line in self.git(source, ["config", "--list"]).splitlines()
        ):
            raise ToolError("私有副本暂不支持执行外部 Git filter。")
        if self.git(source, ["status", "--porcelain", "--untracked-files=normal"]):
            raise ToolError("创建副本前请先保存或提交原工作区改动。")
        base = self.git(source, ["rev-parse", "HEAD"]).strip()
        tree = self.git(source, ["ls-tree", "-r", base])
        if any(line.startswith(("120000 ", "160000 ")) for line in tree.splitlines()):
            raise ToolError("私有副本暂不支持符号链接和子模块。")
        copy_id = secrets.token_hex(8)
        job = self.root / copy_id
        job.mkdir()
        copy = PrivateCopy(
            copy_id,
            job / "repo",
            source,
            base,
            PermissionConfig(additional_read_dirs=permissions.additional_read_dirs),
        )
        self._save(copy, "creating")
        try:
            self.git(
                source, ["clone", "--no-local", "--no-checkout", "--", str(source), str(copy.path)]
            )
            self.git(copy.path, ["checkout", "--detach", base])
            self.git(copy.path, ["remote", "remove", "origin"])
            self._save(copy, "ready")
            return copy
        except BaseException:
            self.cleanup(copy.copy_id)
            raise

    def cleanup(self, copy_id: str) -> None:
        """删除已停止的副本，幂等保留清理记录；拒绝外部路径或目录重定向。"""
        job = self._job(copy_id)
        manifest = job / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        target = job / "repo"
        if data.get("copy_id") != copy_id or data.get("path") != str(target):
            raise ToolError("副本登记身份不匹配，未删除。")
        if target.resolve() != target.absolute() or target.is_symlink():
            raise ToolError("副本路径被重定向，未删除。")
        if target.exists():
            # 只读 Git 对象需要先解除只读标记；rmtree 不跟随目录链接。
            def writable(function: Callable[..., object], path: str, error: object) -> None:
                os.chmod(path, 0o700)
                function(path)

            shutil.rmtree(target, onerror=writable)
        data["status"] = "cleaned"
        self._write(manifest, data)

    def recover_abandoned(self) -> list[str]:
        """宿主重启且获得主会话锁后清理遗留副本，不启动或重跑任何子任务。"""
        cleaned = []
        for manifest in self.root.glob("*/manifest.json"):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("status") != "cleaned":
                self.cleanup(manifest.parent.name)
                cleaned.append(manifest.parent.name)
        return cleaned

    def git(self, cwd: Path, args: list[str]) -> str:
        """宿主固定参数的本地 Git 调用；禁用继承配置与 hooks，30 秒超时。"""
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        command = [
            "git",
            "-c",
            "core.hooksPath=",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "init.templateDir=",
            "-C",
            str(cwd),
            *args,
        ]
        try:
            result = subprocess.run(command, capture_output=True, env=env, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError("私有副本 Git 操作未完成。") from error
        if result.returncode:
            raise ToolError("私有副本 Git 操作失败；未自动重试。")
        return result.stdout.decode("utf-8", errors="replace")

    def _job(self, copy_id: str) -> Path:
        if len(copy_id) != 16 or any(char not in "0123456789abcdef" for char in copy_id):
            raise ToolError("无效副本 ID。")
        job = self.root / copy_id
        if job.resolve() != job.absolute() or job.is_symlink():
            raise ToolError("副本目录被重定向。")
        return job

    def _save(self, copy: PrivateCopy, status: str) -> None:
        data = asdict(copy)
        data.update(
            path=str(copy.path),
            source=str(copy.source),
            status=status,
            rules_root=str(copy.rules_root),
            memory_identity=str(copy.memory_identity),
            permissions={
                "additional_read_dirs": [str(p) for p in copy.permissions.additional_read_dirs],
                "additional_write_dirs": [],
            },
        )
        self._write(self._job(copy.copy_id) / "manifest.json", data)

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        """生命周期记录先写临时文件并落盘，再原子替换。"""
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
