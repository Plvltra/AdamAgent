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
from adam.contracts.checkpoints import EditCheckpoint
from adam.contracts.errors import ToolError
from adam.contracts.models import CreatePlan, DeletePlan, EditPlan, FileChangePlan
from adam.tool_calls.edit_plan import EditConflictError, plan_create, plan_edit, plan_restore
from adam.tool_calls.file_evidence import FileResult, FileStateError
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import read_snapshot


class EditTools:
    """组织读取、计划和批准；模型接入由注册表负责，不在此处请求模型。"""

    def __init__(
        self,
        policy: PathPolicy,
        limits: ToolLimits,
        confirm: Callable[[FileChangePlan], bool] | None = None,
    ) -> None:
        self.policy, self.limits, self.confirm = policy, limits, confirm

    def prepare(self, args: dict[str, Any]) -> EditPlan:
        """校验写入范围并规划；expected_version 来自先前 read_file，不修改文件。"""
        path = self.policy.resolve_write(args["path"])
        snapshot = read_snapshot(path, self.limits.max_text_file_bytes)
        try:
            return plan_edit(
                snapshot,
                args["old_text"],
                args["new_text"],
                args["expected_version"],
                self.limits.max_text_file_bytes,
            )
        except FileStateError:
            raise
        except ToolError as error:
            # 规划已拿到完整原字节；同一错误遇到新版本不算重复原地失败。
            raise FileStateError(str(error), version=snapshot.version) from error

    def prepare_create(self, args: dict[str, Any]) -> CreatePlan:
        """检查目标缺失与独立写权限，固定创建候选和diff；不批准、不写入。"""
        path = self.policy.resolve_create(args["path"])
        return plan_create(path, args["content"], self.limits.max_text_file_bytes)

    def confirm_create(self, plan: CreatePlan) -> bool:
        """展示固定创建计划，并在批准前后复核权限、父目录与目标缺失；不写入。

        没有回调或明确拒绝时返回False；批准期间发生路径变化则抛ToolError。
        True只批准当前候选，不锁定路径，也不代表创建成功；发布时仍须原子拒绝覆盖。
        """
        self._check_create_target(plan)
        if self.confirm is None or not self.confirm(plan):
            return False
        self._check_create_target(plan)
        return True

    def prepare_restore(self, checkpoint: EditCheckpoint) -> EditPlan | DeletePlan:
        """按当前写权限核对修改后版本，规划恢复原字节或删除已创建文件；无写入副作用。"""
        try:
            path = self.policy.resolve_write(str(checkpoint.path))
            return plan_restore(read_snapshot(path, self.limits.max_text_file_bytes), checkpoint)
        except OSError as error:
            raise ToolError("撤销目标无法读取，请检查路径和权限；未修改文件。") from error

    def replace_text(
        self, args: dict[str, Any], record: Callable[[str, dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """先校验路径、旧版本与唯一替换，再交给共同的批准和写入流程。"""
        return self.execute_plan(self.prepare(args), record)

    def create_file(
        self, args: dict[str, Any], record: Callable[[str, dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """检查缺失目标与完整内容，再复用批准、检查点及不覆盖发布流程。"""
        return self.execute_plan(self.prepare_create(args), record)

    def execute_plan(
        self,
        plan: EditPlan | CreatePlan,
        record: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """记录已校验计划后确认；检查点与开始记录成功才写入，结果记录成功才回传。

        plan由prepare、prepare_create或同等宿主校验生成，不能直接接收模型构造的计划。
        没有确认回调则拒绝。编辑日志失败向上传播，未知结果不标记失败或自动重试。
        本地组件可不传 record，此时不保存检查点；模型执行由 ToolExecutor 提供持久化回调。
        """
        target_version = sha256(plan.candidate).hexdigest()
        details = {
            "path": str(plan.path),
            "before_version": plan.before_version,
            "target_version": target_version,
            "diff": plan.diff,
            **({"operation": "create"} if isinstance(plan, CreatePlan) else {}),
        }
        if record:
            record("edit_planned", details)
        started = monotonic_ns()
        result: dict[str, Any] = {"path": str(plan.path), "after_version": None, "error": None}
        approved = (
            self.confirm_create(plan)
            if isinstance(plan, CreatePlan)
            else self.confirm is not None and self.confirm(plan)
        )
        if not approved:
            result["status"] = "denied"
        else:
            # 文件和日志不是一个事务；开始后未记录结束时一律保留 unknown。
            if record:
                record(
                    "edit_checkpoint",
                    {"original": plan.original, "max_bytes": self.limits.max_text_file_bytes},
                )
                record("edit_started", {})
            try:
                if isinstance(plan, CreatePlan):
                    self.apply_create(plan)
                else:
                    self.apply(plan)
            except ToolError as error:
                if isinstance(error, FileStateError):
                    result = FileResult(result, version=error.version)
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

    def apply_create(self, plan: CreatePlan) -> None:
        """发布已批准的创建候选；目标存在时绝不覆盖，不自动创建父目录或重试。

        调用者负责批准固定计划和保存开始记录。候选先在同目录完整写入并flush/fsync，
        再用硬链接发布新名字；不支持硬链接的文件系统直接失败，不退回普通覆盖写入。
        发布前复核权限和缺失状态，但路径检查不是抵御恶意目录替换的操作系统沙箱。
        """
        temporary: Path | None = None
        try:
            self._check_create_target(plan)
            with tempfile.NamedTemporaryFile(
                dir=plan.path.parent, prefix=".adam-create-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(plan.candidate)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_create_target(plan)
            # 与replace不同，link在目标已存在时失败；批准后的同名竞争不会覆盖对方。
            os.link(temporary, plan.path)
        except FileExistsError as error:
            raise EditConflictError("创建目标已经出现，未覆盖；请重新检查文件。") from error
        except OSError as error:
            raise ToolError("文件创建失败；未自动重试或覆盖，请检查权限和文件系统支持。") from error
        finally:
            if temporary is not None:
                try:
                    # 发布后两个名字共享文件，不能chmod临时名字，否则会修改目标属性。
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 清理失败不掩盖原结果；遗留临时名字不代表另一份创建或自动恢复动作。
                    pass

    def apply_restore(self, plan: EditPlan | DeletePlan) -> None:
        """执行已批准撤销；原替换复用apply，创建撤销只删除仍匹配版本的普通文件。

        调用方负责从成功检查点规划、批准及保存undo_started。删除前复核当前权限与内容，
        复核和unlink不是跨进程比较删除事务；不提供绕过检查点的模型删除工具。
        """
        if isinstance(plan, EditPlan):
            self.apply(plan)
            return
        try:
            self._check_current(plan)
            plan.path.unlink()
        except OSError as error:
            raise ToolError("撤销创建时删除失败；未自动重试，请检查路径和权限。") from error

    def _check_create_target(self, plan: CreatePlan) -> None:
        """每次按当前策略检查缺失目标；返回后仍可能出现同名目录项，不充当写锁。"""
        if self.policy.resolve_create(str(plan.path)) != plan.path:
            raise EditConflictError("创建目标路径已经变化，请重新生成并确认计划。")

    def _check_current(self, plan: EditPlan | DeletePlan) -> None:
        """批准后的目标仍须同路径、同版本，防止替换或撤销删除已观察到的外部编辑。"""
        path = self.policy.resolve_write(str(plan.path))
        if path != plan.path:
            raise EditConflictError("批准后文件已变化，请重新读取并确认新的 diff。")
        snapshot = read_snapshot(path, self.limits.max_text_file_bytes)
        if snapshot.version != plan.before_version:
            raise EditConflictError(
                "批准后文件已变化，请重新读取并确认新的 diff。", version=snapshot.version
            )
