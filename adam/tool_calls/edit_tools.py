"""确认后执行单文件修改；规划和写入均检查权限，写入前重新核对版本。"""

import os
import stat
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import monotonic_ns
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolError
from adam.contracts.models import EditPlan
from adam.tool_calls.edit_plan import EditConflictError, plan_edit
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot


class EditTools:
    """组织读取、计划和批准；模型接入由注册表负责，不在此处请求模型。"""

    def __init__(
        self,
        policy: PathPolicy,
        limits: ToolLimits,
        confirm: Callable[[EditPlan], bool] | None = None,
    ) -> None:
        self.policy, self.limits, self.confirm = policy, limits, confirm

    def prepare(self, args: dict[str, Any]) -> EditPlan:
        """校验写入范围并规划；expected_version 来自先前 read_file，不修改文件。"""
        path = self.policy.resolve_write(args["path"])
        return plan_edit(
            read_snapshot(path, self.limits.max_text_file_bytes),
            args["old_text"],
            args["new_text"],
            args["expected_version"],
            self.limits.max_text_file_bytes,
        )

    def replace_text(
        self, args: dict[str, Any], record: Callable[[str, dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """记录计划后确认；开始记录成功才写入，结果记录成功才回传。

        没有确认回调则拒绝。编辑日志失败向上传播，未知结果不标记失败或自动重试。
        本地组件可不传 recorder；模型执行由 ToolExecutor 提供持久化回调。
        """
        plan = self.prepare(args)
        target_version = sha256(plan.candidate).hexdigest()
        details = {
            "path": str(plan.path),
            "before_version": plan.before_version,
            "target_version": target_version,
            "diff": plan.diff,
        }
        if record:
            record("edit_planned", details)
        started = monotonic_ns()
        result: dict[str, Any] = {"path": str(plan.path), "after_version": None, "error": None}
        if self.confirm is None or not self.confirm(plan):
            result["status"] = "denied"
        else:
            # 文件和日志不是一个事务；开始后未记录结束时一律保留 unknown。
            if record:
                record("edit_started", {})
            try:
                self.apply(plan)
            except ToolError as error:
                result.update(
                    status="conflict" if isinstance(error, EditConflictError) else "failed",
                    ok=False,
                    error=str(error),
                )
                if record is None:
                    raise
            else:
                result.update(
                    status="applied",
                    before_version=plan.before_version,
                    after_version=target_version,
                )
        result["duration_ms"] = max(0, (monotonic_ns() - started) // 1_000_000)
        if record:
            record("edit_finished", result)
        return result

    def apply(self, plan: EditPlan) -> None:
        """写入已批准的计划；冲突或替换前 IO 失败保留原文件。

        调用者负责批准这个不可变计划。临时文件在同目录创建并 flush/fsync，
        替换前再次检查授权与内容版本；检查和替换不构成跨进程事务。
        """
        temporary: Path | None = None
        try:
            self._check_current(plan)
            mode = stat.S_IMODE(plan.path.stat().st_mode)
            with tempfile.NamedTemporaryFile(
                dir=plan.path.parent, prefix=".adam-edit-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(plan.candidate)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            self._check_current(plan)
            os.replace(temporary, plan.path)
        except OSError as error:
            raise ToolError("文件写入失败；未自动重试，请检查路径和权限。") from error
        finally:
            if temporary is not None:
                # 清理失败不遮蔽原异常；崩溃遗留临时文件也不会被当作成功修改。
                try:
                    temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                    temporary.unlink()
                except OSError:
                    pass

    def _check_current(self, plan: EditPlan) -> None:
        """批准后的目标仍须是同一路径、同一版本，不能覆盖外部编辑。"""
        path = self.policy.resolve_write(str(plan.path))
        if (
            path != plan.path
            or read_snapshot(path, self.limits.max_text_file_bytes).version != plan.before_version
        ):
            raise EditConflictError("批准后文件已变化，请重新读取并确认新的 diff。")
