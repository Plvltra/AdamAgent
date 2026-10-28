"""固定参数的只读 Git 查询；共享权限检查和受限子进程入口。"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from adam.errors import ToolError
from adam.settings import ToolLimits
from adam.tool_calls.policy import PathPolicy


class GitTools:
    def __init__(self, policy: PathPolicy, limits: ToolLimits) -> None:
        self.policy = policy
        self.limits = limits

    def git_status(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """查询已跟踪文件的短格式状态。"""
        return self._query("git_status", path, ["status", "--short", "--untracked-files=no"])

    def git_diff(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """查询工作区差异；staged=True 时查询暂存区，禁用外部 diff。"""
        command = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if args.get("staged", False):
            command.append("--cached")
        return self._query("git_diff", path, command)

    def git_log(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        """返回最近 limit 条提交摘要，limit 范围为 1 到 50。"""
        limit = args.get("limit", 10)
        if not 1 <= limit <= 50:
            raise ToolError("Git日志条数必须在1到50之间。")
        return self._query("git_log", path, ["log", f"-{limit}", "--format=%h %s", "--no-decorate"])

    def _query(self, name: str, path: Path, command: list[str]) -> dict[str, Any]:
        """校验仓库后运行固定命令；每个子进程独立计时，输出有界。"""
        # 只接受包含普通.git目录的仓库根；worktree/gitdir指向外部需另行支持。
        git_dir = path / ".git"
        if not path.is_dir() or not git_dir.is_dir() or git_dir.is_symlink():
            raise ToolError("Git工具需要普通仓库根目录；暂不支持外部gitdir/worktree。")
        self.policy.resolve(str(git_dir))
        if (git_dir / "objects" / "info" / "alternates").exists() or (
            git_dir / "commondir"
        ).exists():
            raise ToolError("暂不支持共享或外部Git对象目录。")
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        prefix = [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=",
            "-c",
            "diff.ignoreSubmodules=all",
            "-C",
            str(path),
        ]

        def query(arguments: list[str], cap: int) -> tuple[int, bytes]:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    prefix + arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=self.limits.git_timeout_seconds,
                    env=env,
                    check=False,
                )
                output.seek(0)
                return result.returncode, output.read(cap + 1)

        self.policy.resolve(str(git_dir / "objects"))
        if name in {"git_status", "git_diff"}:
            # status/diff也可能触发clean filter；本版拒绝这类仓库，避免执行外部程序。
            status, filters = query(["config", "--name-only", "--get-regexp", r"^filter\."], 4096)
            if status not in (0, 1) or filters:
                raise ToolError("仓库配置了Git过滤器或配置读取失败，暂不支持该只读查询。")
        if name == "git_diff":
            status, files = query(["ls-files", "-z"], self.limits.max_git_path_list_bytes)
            if status or len(files) > self.limits.max_git_path_list_bytes:
                raise ToolError("无法完整检查Git路径权限，请缩小仓库范围。")
            for filename in files.decode("utf-8").split("\0"):
                if filename:
                    self.policy.resolve(str(path / filename), must_exist=False)
        status, data = query(command, self.limits.max_result_chars * 4)
        if status:
            raise ToolError("Git查询失败；请检查仓库状态或是否存在提交。")
        text = data.decode("utf-8", errors="replace")
        return {
            "text": text[: self.limits.max_result_chars // 2],
            "truncated": len(text) > self.limits.max_result_chars // 2,
        }
