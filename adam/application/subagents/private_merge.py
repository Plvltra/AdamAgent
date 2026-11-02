"""将停止后的私有副本差异冻结为提案，审批后用现有文件编辑器合并。"""

import json
from dataclasses import asdict
from difflib import unified_diff
from hashlib import sha256
from pathlib import Path
from typing import Any

from adam.application.subagents.workspaces import PrivateCopy, PrivateWorkspaces
from adam.contracts.errors import ToolError
from adam.contracts.models import CreatePlan, EditPlan
from adam.contracts.verification import TaskVerifier
from adam.tool_calls.edit_tools import EditTools


class PrivateMerge:
    """由宿主传入真实副本身份；一份提案只尝试应用一次，部分写入如实记录。

    edits 保留主项目当前权限和审批回调，verifier 是宿主指定的跨文件判据。
    不提交 Git、不自动解决冲突、不在验证失败时静默回滚外部改动。
    """

    def __init__(self, copies: PrivateWorkspaces, edits: EditTools, verifier: TaskVerifier) -> None:
        self.copies, self.edits, self.verifier = copies, edits, verifier

    def collect(self, copy: PrivateCopy) -> Path:
        """从真实 Git 基线和副本终态生成文本提案；调用前须等待子线程结束。"""
        if copy.path != self.copies.root / copy.copy_id / "repo":
            raise ToolError("副本不属于此会话。")
        proposal = copy.path.parent / "proposal.json"
        if proposal.exists():
            return proposal
        changed = self.copies.git(
            copy.path,
            ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", copy.base_commit, "--"],
        )
        added = self.copies.git(copy.path, ["ls-files", "--others", "--exclude-standard", "-z"])
        names = sorted(set((changed + added).split("\0")) - {""})
        if not 1 <= len(names) <= 16:
            raise ToolError("提案须包含 1 到 16 个变动文件。")
        tracked = set(
            self.copies.git(
                copy.path, ["ls-tree", "-r", "--name-only", "-z", copy.base_commit]
            ).split("\0")
        )
        changes = []
        for name in names:
            relative = Path(name)
            if (
                relative.is_absolute()
                or relative.drive
                or ".." in relative.parts
                or relative.name.casefold() == "agents.md"
            ):
                raise ToolError("提案路径或规则文件不允许合并。")
            target = copy.path / relative
            if target.resolve() != target.absolute() or not target.is_file():
                raise ToolError("首版不合并删除或链接。")
            raw = target.read_bytes()
            before = (
                self.copies.git(copy.path, ["show", f"{copy.base_commit}:{relative.as_posix()}"])
                if name in tracked
                else None
            )
            after = raw.decode("utf-8")
            if len(raw) > 1048576 or "\0" in after or (before is not None and "\0" in before):
                raise ToolError("提案仅支持 1 MiB 以内的 UTF-8 文本。")
            if before != after:
                changes.append({"path": relative.as_posix(), "before": before, "after": after})
        if not changes:
            raise ToolError("没有可合并的文本改动。")
        data = {
            "copy_id": copy.copy_id,
            "source": str(copy.source),
            "base_commit": copy.base_commit,
            "status": "proposed",
            "changes": changes,
            "results": [],
        }
        self.copies._write(proposal, data)
        return proposal

    def apply(self, proposal: Path) -> dict[str, Any]:
        """固定全组计划后逐文件审批/应用，最后独立验收；不把部分合并算成功。"""
        if proposal.name != "proposal.json" or proposal.parent.parent != self.copies.root:
            raise ToolError("提案不属于此会话。")
        if proposal.resolve() != proposal.absolute():
            raise ToolError("提案路径发生重定向。")
        data = json.loads(proposal.read_text(encoding="utf-8"))
        if data["status"] != "proposed" or Path(data["source"]) != self.edits.policy.workspace:
            raise ToolError("提案已尝试应用或项目身份不符。")
        plans = [self._plan(data["source"], change) for change in data["changes"]]
        data["status"] = "applying"
        self.copies._write(proposal, data)
        try:
            for plan in plans:
                result = self.edits.execute_plan(plan)
                data["results"].append(result)
                self.copies._write(proposal, data)
                if result["status"] != "applied":
                    data["status"] = "incomplete"
                    break
            else:
                verification = self.verifier.check("")
                data["verification"] = asdict(verification)
                data["status"] = (
                    "verified" if verification.status == "passed" else "verification_failed"
                )
        except BaseException:
            data["status"] = "incomplete"
            self.copies._write(proposal, data)
            raise
        self.copies._write(proposal, data)
        return data

    def _plan(self, source: str, change: dict[str, Any]) -> EditPlan | CreatePlan:
        """重新核对路径、主文件版本与大小；冲突在申请批准前失败。"""
        relative = Path(change["path"])
        if (
            relative.is_absolute()
            or relative.drive
            or ".." in relative.parts
            or relative.name.casefold() == "agents.md"
        ):
            raise ToolError("非法提案路径。")
        path = Path(source) / relative
        before, after = change["before"], change["after"]
        raw = after.encode("utf-8")
        if len(raw) > self.edits.limits.max_text_file_bytes:
            raise ToolError("提案超过文件大小限制。")
        if before is None:
            return self.edits.prepare_create({"path": str(path), "content": after})
        target = self.edits.policy.resolve_write(str(path))
        original = before.encode("utf-8")
        if target.read_bytes() != original:
            raise ToolError("主项目文件已变化；提案未获批准、未合并。")
        diff = "".join(
            unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile=str(path),
                tofile=str(path),
            )
        )
        return EditPlan(target, sha256(original).hexdigest(), raw, diff, original)
