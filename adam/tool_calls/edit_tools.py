"""确认后执行单文件修改；规划和写入均检查权限，写入前重新核对版本。"""

import os
import stat
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.tool_calls.edit_plan import EditConflictError, EditPlan, plan_edit
from adam.tool_calls.policy import PathPolicy, ToolError, ToolLimits
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

    def replace_text(self, args: dict[str, Any]) -> dict[str, Any]:
        """只执行明确批准的计划；未提供确认回调时拒绝，返回实际执行状态。"""
        plan = self.prepare(args)
        if self.confirm is None or not self.confirm(plan):
            return {"status": "denied", "path": str(plan.path)}
        self.apply(plan)
        return {
            "status": "applied",
            "path": str(plan.path),
            "before_version": plan.before_version,
            "after_version": sha256(plan.candidate).hexdigest(),
        }

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
