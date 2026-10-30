"""固定参数的只读 Git 查询；共享权限检查和受限子进程入口。"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.processes import terminate_process


class GitTools:
    def __init__(
        self, policy: PathPolicy, limits: ToolLimits, cancel: CancellationToken | None = None
    ) -> None:
        """每次查询独立绑定取消令牌，不与其他并发读取共享可变状态。"""
        self.policy = policy
        self.limits = limits
        self._cancel = cancel

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
        self._check_cancel()
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
            return self._run_query(prefix + arguments, env, cap)

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
                self._check_cancel()
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

    def _run_query(self, argv: list[str], env: dict[str, str], cap: int) -> tuple[int, bytes]:
        """每个固定查询独立限时；取消或异常先回收进程，再关闭结果文件。"""
        self._check_cancel()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=env,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + self.limits.git_timeout_seconds
                while process.poll() is None:
                    self._check_cancel()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, self.limits.git_timeout_seconds)
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                terminate_process(process)
                raise
            output.seek(0)
            return process.returncode, output.read(cap + 1)

    def _check_cancel(self) -> None:
        """取消不转换为 Git 查询失败，交给工具批次统一收尾。"""
        if self._cancel:
            self._cancel.check()
